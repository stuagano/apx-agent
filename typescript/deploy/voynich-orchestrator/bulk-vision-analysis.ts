/**
 * Bulk vision analysis for unanalyzed Voynich herbal folios.
 *
 * Fetches the Yale IIIF manifest to build folio_id → catalog_id mapping,
 * then for each herbal folio not yet in folio_vision_analysis:
 *   1. Downloads the image from Yale collections (public domain)
 *   2. Runs claude-sonnet-4-6 vision analysis
 *   3. Stores structured result in folio_vision_analysis
 *
 * Resumable: skips folios already in the table. Safe to re-run.
 * Rate-limited: 1 image per 3s to avoid hammering Yale + LLM quota.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx bulk-vision-analysis.ts
 *
 * To limit how many folios to process in one run (useful for token lifetime):
 *   MAX_FOLIOS=30 npx tsx bulk-vision-analysis.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const IIIF_MANIFEST_URL = 'https://collections.library.yale.edu/manifests/2002046';
const IIIF_IMAGE_BASE   = 'https://collections.library.yale.edu/iiif/2';
const MODEL_ID          = process.env.VISION_MODEL ?? 'databricks-claude-sonnet-4-6';
const MAX_FOLIOS        = parseInt(process.env.MAX_FOLIOS ?? '200');
const RATE_LIMIT_MS     = parseInt(process.env.RATE_LIMIT_MS ?? '3000');

// ---------------------------------------------------------------------------
// SQL helpers
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '50s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?: { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?: { state?: string; error?: { message?: string } };
  };
  if (data.status?.state === 'FAILED') throw new Error(`SQL failed: ${data.status.error?.message}`);
  const cols = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  return (data.result?.data_array ?? []).map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

async function executeSqlWrite(statement: string): Promise<void> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '50s' }),
  });
  if (!res.ok) throw new Error(`SQL write ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as { status?: { state?: string; error?: { message?: string } } };
  if (data.status?.state === 'FAILED') throw new Error(`SQL write failed: ${data.status.error?.message}`);
}

// ---------------------------------------------------------------------------
// IIIF manifest → folio_id mapping
// ---------------------------------------------------------------------------

interface FolioMapping {
  folioId: string;   // e.g. "f21r"
  catalogId: string; // e.g. "1006114"
  imageUrl: string;  // full IIIF image URL
}

/** Extract all folio IDs mentioned in a manifest label (handles compound labels). */
function labelToFolioIds(label: string): string[] {
  // Find all "NNNr" or "NNNv" patterns in the label
  const matches = [...label.matchAll(/(\d+[rv])/gi)];
  const ids = [...new Set(matches.map((m) => `f${m[1].toLowerCase()}`))];
  return ids;
}

async function fetchIiifManifest(): Promise<Map<string, FolioMapping>> {
  console.log(`Fetching IIIF manifest from ${IIIF_MANIFEST_URL}...`);
  const res = await fetch(IIIF_MANIFEST_URL);
  if (!res.ok) throw new Error(`Manifest fetch failed: ${res.status}`);
  const data = (await res.json()) as {
    items?: Array<{
      label?: Record<string, string[]>;
      items?: Array<{ items?: Array<{ body?: { id?: string } }> }>;
    }>;
  };

  const mapping = new Map<string, FolioMapping>();
  for (const canvas of data.items ?? []) {
    const labelDict = canvas.label ?? {};
    const label = Object.values(labelDict)[0]?.[0] ?? '';
    const bodyId = canvas.items?.[0]?.items?.[0]?.body?.id ?? '';
    const catalogMatch = bodyId.match(/\/iiif\/2\/(\d+)\//);
    if (!catalogMatch) continue;
    const catalogId = catalogMatch[1];
    const imageUrl = `${IIIF_IMAGE_BASE}/${catalogId}/full/1024,/0/default.jpg`;

    // Register ALL folio IDs mentioned in the label (handles "69v and 70r" etc.)
    for (const folioId of labelToFolioIds(label)) {
      if (!mapping.has(folioId)) {
        mapping.set(folioId, { folioId, catalogId, imageUrl });
      }
    }
  }
  console.log(`  ${mapping.size} folio → catalog ID mappings extracted`);
  return mapping;
}

// ---------------------------------------------------------------------------
// Image fetch → base64
// ---------------------------------------------------------------------------

async function fetchImageAsBase64(url: string): Promise<string> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Image fetch ${res.status}: ${url}`);
  const buffer = await res.arrayBuffer();
  return Buffer.from(buffer).toString('base64');
}

// ---------------------------------------------------------------------------
// LLM vision analysis
// ---------------------------------------------------------------------------

const VISION_PROMPT = `You are analyzing a page from the Voynich manuscript (MS 408, Yale Beinecke Library), a 15th-century illustrated herbal manuscript in an unknown script.

This folio is from the herbal section. Analyze the plant illustration and the layout of the text on the page.

Return a JSON object with exactly these fields:

{
  "subject_candidates": [
    {
      "name": "Common name / possible identification",
      "latin": "Genus species or family",
      "confidence": 0.0-1.0,
      "reasoning": "Brief reasoning based on visual features"
    }
  ],
  "botanical_features": ["list", "of", "visible", "plant", "features"],
  "visual_description": "2-3 sentence detailed description of what is depicted",
  "spatial_layout": {
    "text_regions": [
      {"position": "top-left|top-right|left|right|bottom|center", "role": "label|description|unknown", "estimated_lines": N}
    ]
  },
  "expected_terms": {
    "latin": ["word1", "word2", ...],
    "italian": ["word1", "word2", ...]
  }
}

For subject_candidates: provide 1-3 candidates ranked by confidence. Be honest about uncertainty — many folios are ambiguous or depict fantastical plants.
For expected_terms: list ~30 medieval Latin and Italian words you would expect in a herbal text about this plant (plant parts, properties, uses, preparations). These should be actual medieval vocabulary words.
For spatial_layout: describe where text appears relative to the illustration.

Return ONLY the JSON object, no other text.`;

interface VisionResult {
  subject_candidates: Array<{ name: string; latin: string; confidence: number; reasoning: string }>;
  botanical_features: string[];
  visual_description: string;
  spatial_layout: object;
  expected_terms: { latin: string[]; italian: string[] };
}

async function analyzeImage(imageUrl: string): Promise<VisionResult | null> {
  const host = resolveHost();
  const token = await resolveToken();

  // Databricks endpoint uses OpenAI-compatible format: image_url with data URI
  const imgRes = await fetch(imageUrl);
  if (!imgRes.ok) throw new Error(`Image fetch ${imgRes.status}: ${imageUrl}`);
  const imgBuffer = await imgRes.arrayBuffer();
  const dataUrl = `data:image/jpeg;base64,${Buffer.from(imgBuffer).toString('base64')}`;

  const res = await fetch(`${host}/serving-endpoints/${MODEL_ID}/invocations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      messages: [{
        role: 'user',
        content: [
          { type: 'image_url', image_url: { url: dataUrl } },
          { type: 'text', text: VISION_PROMPT },
        ],
      }],
      max_tokens: 1500,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    console.warn(`    LLM ${res.status}: ${body.slice(0, 200)}`);
    return null;
  }

  const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
  const content = data.choices?.[0]?.message?.content ?? '';
  try {
    const cleaned = content.replace(/```json?\s*/g, '').replace(/```/g, '').trim();
    return JSON.parse(cleaned) as VisionResult;
  } catch {
    console.warn(`    JSON parse failed. Raw: ${content.slice(0, 200)}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// DB write
// ---------------------------------------------------------------------------

function escape(s: string): string {
  return s.replace(/'/g, "''");
}

async function storeResult(
  folioId: string,
  imageUrl: string,
  volumePath: string,
  result: VisionResult,
): Promise<void> {
  const sc = escape(JSON.stringify(result.subject_candidates));
  const bf = escape(JSON.stringify(result.botanical_features));
  const vd = escape(result.visual_description);
  const sl = escape(JSON.stringify(result.spatial_layout));
  const et = escape(JSON.stringify(result.expected_terms));

  await executeSqlWrite(`
    INSERT INTO serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
      (folio_id, section, image_url, volume_path, subject_candidates, spatial_layout,
       visual_description, botanical_features, expected_terms, model_id, analyzed_at, prompt_version)
    VALUES (
      '${escape(folioId)}', 'herbal', '${escape(imageUrl)}', '${escape(volumePath)}',
      '${sc}', '${sl}', '${vd}', '${bf}', '${et}',
      '${MODEL_ID}', current_timestamp(), 'v1'
    )
  `);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('bulk-vision-analysis — extending folio_vision_analysis to all herbal folios');
  console.log(`Model: ${MODEL_ID}, MAX_FOLIOS=${MAX_FOLIOS}, rate limit=${RATE_LIMIT_MS}ms`);
  console.log('');

  // Build IIIF manifest mapping
  const iiifMap = await fetchIiifManifest();

  // Get already-analyzed folios
  const existingRows = await executeSql(
    `SELECT folio_id FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section = 'herbal'`
  );
  const analyzed = new Set(existingRows.map((r) => r.folio_id ?? ''));
  console.log(`  ${analyzed.size} folios already analyzed`);

  // Get all herbal folio IDs from EVA corpus
  const evaRows = await executeSql(
    `SELECT DISTINCT folio_id FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section = 'herbal' ORDER BY folio_id`
  );
  const allFolios = evaRows.map((r) => r.folio_id ?? '').filter(Boolean);
  console.log(`  ${allFolios.length} total herbal folios in EVA corpus`);

  const todo = allFolios.filter((id) => !analyzed.has(id));
  console.log(`  ${todo.length} folios need analysis`);
  console.log(`  Processing up to ${MAX_FOLIOS} this run`);
  console.log('');

  const batch = todo.slice(0, MAX_FOLIOS);
  let success = 0, failed = 0, noImage = 0;

  for (let i = 0; i < batch.length; i++) {
    const folioId = batch[i];
    const mapping = iiifMap.get(folioId);

    process.stdout.write(`[${i + 1}/${batch.length}] ${folioId} `);

    let resolvedMapping = mapping;
    if (!resolvedMapping) {
      // Try stripping trailing digit for sub-folios (e.g. f102r1 → f102r)
      const baseId = folioId.replace(/\d+$/, '');
      const baseMapping = iiifMap.get(baseId);
      if (!baseMapping) {
        process.stdout.write('— no IIIF mapping, skipping\n');
        noImage++;
        continue;
      }
      process.stdout.write(`(using ${baseId} image) `);
      resolvedMapping = { ...baseMapping, folioId };
    }

    const { imageUrl } = resolvedMapping;
    const volumePath = `/Volumes/serverless_stable_qh44kx_catalog/voynich/images/herbal/${folioId}.jpg`;

    try {
      process.stdout.write('fetching+analyzing... ');
      const result = await analyzeImage(imageUrl);

      if (!result) {
        process.stdout.write('FAILED (LLM parse error)\n');
        failed++;
      } else {
        await storeResult(folioId, imageUrl, volumePath, result);
        const topCandidate = result.subject_candidates?.[0];
        process.stdout.write(`OK — ${topCandidate?.name ?? 'unknown'} (${((topCandidate?.confidence ?? 0) * 100).toFixed(0)}%)\n`);
        success++;
      }
    } catch (err) {
      process.stdout.write(`ERROR: ${(err as Error).message.slice(0, 80)}\n`);
      failed++;
    }

    // Rate limit between calls
    if (i < batch.length - 1) await new Promise((r) => setTimeout(r, RATE_LIMIT_MS));
  }

  console.log('');
  console.log(`=== DONE ===`);
  console.log(`  Success: ${success}  Failed: ${failed}  No image mapping: ${noImage}`);
  console.log(`  Total analyzed now: ${analyzed.size + success}`);

  if (todo.length > batch.length) {
    console.log(`  Remaining: ${todo.length - batch.length} folios. Re-run with a fresh token.`);
  } else {
    console.log('  All herbal folios processed!');
  }
}

main().catch((err) => {
  console.error('[bulk-vision-analysis] fatal:', err);
  process.exit(1);
});
