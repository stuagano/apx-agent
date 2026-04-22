/**
 * Voynich Grounder — image grounding fitness scorer for the Voynich Manuscript.
 *
 * Scores how well a decoded text passage matches what is depicted in the
 * associated folio illustration. Reads pre-cached vision analysis from the
 * folio_vision_analysis Delta table (no vision API calls in the hot loop).
 *
 * Required environment variables:
 *   DATABRICKS_HOST          Workspace URL
 *   DATABRICKS_WAREHOUSE_ID  SQL warehouse for reading folio_vision_analysis
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.cloud.databricks.com \
 *   DATABRICKS_TOKEN=your-token \
 *   DATABRICKS_WAREHOUSE_ID=your-warehouse-id \
 *   npx tsx app.ts
 */

import express from 'express';
import { z } from 'zod';
import {
  createAgentPlugin,
  createDiscoveryPlugin,
  createMcpPlugin,
  createDevPlugin,
  defineTool,
  resolveToken,
  resolveHost,
} from '../../../src/index.js';

// ---------------------------------------------------------------------------
// SQL helper
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');

  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      warehouse_id: warehouseId,
      statement,
      wait_timeout: '30s',
    }),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`SQL ${res.status}: ${text}`);
  }

  const data = (await res.json()) as {
    result?: { data_array?: string[][] };
    manifest?: { columns?: Array<{ name: string }> };
    status?: { state?: string; error?: { message?: string } };
  };

  if (data.status?.state === 'FAILED') {
    throw new Error(`SQL failed: ${data.status.error?.message}`);
  }

  const columns = data.manifest?.columns?.map((c) => c.name) ?? [];
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => {
    const obj: Record<string, string> = {};
    columns.forEach((col, i) => {
      obj[col] = row[i];
    });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// Vision analysis cache
// ---------------------------------------------------------------------------

const ANALYSIS_TABLE =
  process.env.VISION_TABLE ?? 'serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis';

interface FolioAnalysis {
  folio_id: string;
  subject_candidates: Array<{
    name: string;
    latin?: string;
    confidence: number;
    reasoning?: string;
  }>;
  spatial_layout: {
    text_regions: Array<{
      position: string;
      role: string;
      estimated_lines: number;
    }>;
  };
  visual_description: string;
  botanical_features: string[];
  expected_terms: Record<string, string[]>;
}

let analysisCache: FolioAnalysis[] | null = null;

async function loadAnalyses(): Promise<FolioAnalysis[]> {
  if (analysisCache) return analysisCache;

  const rows = await executeSql(
    `SELECT folio_id, subject_candidates, spatial_layout, visual_description,
            botanical_features, expected_terms
     FROM ${ANALYSIS_TABLE}
     WHERE section = 'herbal'
     ORDER BY folio_id`,
  );

  analysisCache = rows.map((r) => ({
    folio_id: r.folio_id,
    subject_candidates: JSON.parse(r.subject_candidates || '[]'),
    spatial_layout: JSON.parse(r.spatial_layout || '{"text_regions":[]}'),
    visual_description: r.visual_description || '',
    botanical_features: JSON.parse(r.botanical_features || '[]'),
    expected_terms: JSON.parse(r.expected_terms || '{}'),
  }));

  return analysisCache;
}

// ---------------------------------------------------------------------------
// Scoring logic
// ---------------------------------------------------------------------------

function scoreOverlap(decodedText: string, expectedTerms: string[]): number {
  if (expectedTerms.length === 0) return 0;

  const decoded = decodedText.toLowerCase().replace(/[^a-z\s]/g, ' ');
  const tokens = decoded.split(/\s+/).filter((t) => t.length > 2);
  if (tokens.length === 0) return 0;

  let matchScore = 0;
  for (const term of expectedTerms) {
    const termLower = term.toLowerCase();

    // Exact token match
    if (tokens.includes(termLower)) {
      matchScore += 1.0;
      continue;
    }

    // Substring match (decoded contains the expected term)
    if (decoded.includes(termLower)) {
      matchScore += 0.7;
      continue;
    }

    // Partial stem match (first 4+ chars match)
    if (termLower.length >= 4) {
      const stem = termLower.slice(0, Math.min(termLower.length, 5));
      if (tokens.some((t) => t.startsWith(stem))) {
        matchScore += 0.4;
        continue;
      }
    }
  }

  // Normalize by number of expected terms, cap at 1.0
  return Math.min(1.0, matchScore / expectedTerms.length);
}

// ---------------------------------------------------------------------------
// Tool: score_image_grounding
// ---------------------------------------------------------------------------

const scoreImageGrounding = defineTool({
  name: 'score_image_grounding',
  description:
    'Score how well a decoded Voynich text passage matches what is depicted in ' +
    'the associated herbal folio illustrations. Reads cached vision analysis — ' +
    'no vision API calls. Returns a grounding score from 0 (no match) to 1 (strong match).',
  parameters: z.object({
    decoded_text: z
      .string()
      .describe('The decoded/translated text passage to evaluate.'),
    source_language: z
      .string()
      .describe('The candidate source language (e.g., latin, italian, hebrew).'),
    section: z
      .string()
      .default('herbal')
      .describe('Manuscript section (herbal for Phase 1).'),
    folio_id: z
      .string()
      .optional()
      .describe('Specific folio to score against, or omit to score against all herbal folios.'),
  }),
  handler: async ({
    decoded_text,
    source_language,
    folio_id,
  }: {
    decoded_text: string;
    source_language: string;
    section?: string;
    folio_id?: string;
  }) => {
    const analyses = await loadAnalyses();
    const targets = folio_id
      ? analyses.filter((a) => a.folio_id === folio_id)
      : analyses;

    if (targets.length === 0) {
      return { grounding: 0, error: 'No folio analyses found' };
    }

    let bestScore = 0;
    let bestFolio = '';
    let bestDepicted = '';
    let bestMatched: string[] = [];

    for (const analysis of targets) {
      // Get or generate expected terms from subject candidates + botanical features
      let terms = analysis.expected_terms[source_language];
      if (!terms || terms.length === 0) {
        terms = [];
        for (const candidate of analysis.subject_candidates) {
          if (candidate.name) terms.push(candidate.name);
          if (candidate.latin) {
            terms.push(candidate.latin);
            // Add genus name alone (first word of binomial)
            const genus = candidate.latin.split(' ')[0];
            if (genus) terms.push(genus);
          }
        }
        terms.push(...analysis.botanical_features);
      }

      const score = scoreOverlap(decoded_text, terms);
      if (score > bestScore) {
        bestScore = score;
        bestFolio = analysis.folio_id;
        bestDepicted =
          analysis.subject_candidates[0]?.name ?? analysis.visual_description.slice(0, 50);
        bestMatched = terms.filter(
          (t) => decoded_text.toLowerCase().includes(t.toLowerCase()),
        );
      }
    }

    return {
      grounding: Math.round(bestScore * 1000) / 1000,
      best_folio: bestFolio,
      depicted: bestDepicted,
      matched_terms: bestMatched,
      folios_scored: targets.length,
    };
  },
});

// ---------------------------------------------------------------------------
// Agent plugin
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich Grounder, a visual grounding specialist for the Voynich Manuscript.',
    'Use the score_image_grounding tool to evaluate whether a decoded text passage',
    'matches what is depicted in the manuscript herbal illustrations.',
    '',
    'When you receive a hypothesis object, extract decoded_text and source_language,',
    'then call score_image_grounding and respond with ONLY a JSON object:',
    '  { "grounding": <0-1> }',
    '',
    'Do not add explanations. Respond with ONLY the JSON object.',
  ].join('\n'),
  tools: [scoreImageGrounding],
});

const agentExports = () => agentPlugin.exports();

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  {
    name: 'voynich-grounder',
    description: 'Image grounding fitness scorer for Voynich Manuscript decoded text',
  },
  agentExports,
);
discoveryPlugin.setup();

const mcpPlugin = createMcpPlugin({}, agentExports);
mcpPlugin.setup().catch(console.error);

const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
mcpPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const port = parseInt(process.env.PORT ?? '8004');
const server = app.listen(port, () => {
  console.log(`Voynich Grounder running at http://localhost:${port}`);
  console.log(`  /responses               — agent endpoint (Responses API)`);
  console.log(`  /.well-known/agent.json  — A2A discovery card`);
  console.log(`  /mcp                     — MCP server`);
  console.log(`  /_apx/agent              — dev chat UI`);
  console.log(`  /_apx/tools              — tool inspector`);
});
server.timeout = 180_000;
server.keepAliveTimeout = 90_000;
