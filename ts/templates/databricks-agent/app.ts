/**
 * {{AGENT_DISPLAY_NAME}} — Databricks App
 *
 * LLM calls go directly to Databricks FMAPI via fetch().
 * Auth via DATABRICKS_TOKEN env var (managed identity on Databricks Apps).
 *
 * Run locally:
 *   DATABRICKS_HOST=https://your-workspace.cloud.databricks.com \
 *   DATABRICKS_TOKEN=your-token \
 *   npx tsx app.ts
 */

import express from 'express';
import { runAgent } from './src/fmapi.js';
import { ALL_TOOLS } from './src/tools.js';
import { setApiToken } from './src/databricks.js';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const MODEL = 'databricks-claude-sonnet-4-6';
const INSTRUCTIONS =
  '{{AGENT_INSTRUCTIONS}}';

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

// Forward per-request auth to Databricks REST client
app.use((req, _res, next) => {
  const oboToken = (req.headers['x-forwarded-access-token'] as string) ?? '';
  const bearerToken = ((req.headers.authorization as string) ?? '').replace(/^Bearer\s+/i, '');
  if (oboToken || bearerToken) setApiToken(oboToken || bearerToken);
  next();
});

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------

app.get('/api/agent/health', (_req, res) => {
  res.json({ status: 'ok' });
});

app.get('/api/version', (_req, res) => {
  res.json({ version: '1.0.0' });
});

// Responses API — primary agent endpoint
app.post('/responses', async (req, res) => {
  try {
    const input = req.body.input;
    const messages = typeof input === 'string'
      ? [{ role: 'user' as const, content: input }]
      : Array.isArray(input)
        ? input.map((m: any) => ({
            role: (m.role ?? 'user') as 'user',
            content: typeof m.content === 'string'
              ? m.content
              : Array.isArray(m.content)
                ? m.content.filter((p: any) => p.type === 'input_text' || p.type === 'text').map((p: any) => p.text ?? '').join(' ')
                : String(m.content ?? ''),
          }))
        : [{ role: 'user' as const, content: JSON.stringify(input) }];

    const text = await runAgent({
      model: MODEL,
      instructions: INSTRUCTIONS,
      messages,
      tools: ALL_TOOLS,
    });

    res.json({
      id: `resp_${Date.now()}`,
      object: 'response',
      status: 'completed',
      output: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text }] }],
      output_text: text,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error('[/responses] Error:', message);
    res.status(500).json({ error: message });
  }
});

// A2A discovery card
app.get('/.well-known/agent.json', (req, res) => {
  const base = `${req.protocol}://${req.get('host')}`;
  res.json({
    schemaVersion: '1.0',
    name: '{{AGENT_NAME}}',
    description: '{{AGENT_DESCRIPTION}}',
    url: base,
    protocolVersion: '0.3.0',
    capabilities: { streaming: false, multiTurn: true },
    authentication: { schemes: ['bearer'], credentials: 'same_origin' },
    skills: ALL_TOOLS.map((t) => ({ id: t.name, name: t.name, description: t.description })),
  });
});

// Individual tool endpoints
for (const tool of ALL_TOOLS) {
  app.post(`/api/agent/tools/${tool.name}`, async (req, res) => {
    try {
      const result = await tool.handler(req.body);
      res.json(result);
    } catch (err) {
      res.status(500).json({ error: err instanceof Error ? err.message : String(err) });
    }
  });
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

const port = parseInt(process.env.PORT ?? '8000');
app.listen(port, () => {
  console.log(`{{AGENT_DISPLAY_NAME}} running at http://localhost:${port}`);
  console.log(`  POST /responses             — Responses API`);
  console.log(`  GET  /.well-known/agent.json — A2A discovery`);
  console.log(`  GET  /api/agent/health       — Health check`);
});
