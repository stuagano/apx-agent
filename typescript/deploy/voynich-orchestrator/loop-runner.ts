/**
 * Standalone theory-loop runner — one strategy, runs forever, writes structured
 * logs to UC. Deployed as a Databricks App separate from the dashboard.
 *
 * Picks strategy from CIPHER_FOCUS / LANGUAGE_FOCUS env vars (already honored
 * by pickNextStrategy in theory-loop.ts). Exposes a /health endpoint so the
 * App platform can verify liveness.
 */

import express from 'express';
import { runTheoryLoop } from './theory-loop.ts';
import { resolveToken, resolveHost } from './appkit-agent/index.mjs';

const APP_NAME = process.env.DATABRICKS_APP_NAME ?? process.env.APP_NAME ?? 'voynich-loop';
const CIPHER = process.env.CIPHER_FOCUS ?? 'any';
const LANG = process.env.LANGUAGE_FOCUS ?? 'any';
const BURSTS_PER_RUN = parseInt(process.env.BURSTS_PER_RUN ?? '1');
const WAREHOUSE_ID = process.env.DATABRICKS_WAREHOUSE_ID ?? '';
const LOG_TABLE = 'serverless_stable_qh44kx_catalog.voynich.loop_logs';

// Buffer log lines and flush in batches — INSERT-per-line would saturate the warehouse.
let logBuffer: string[] = [];
let flushTimer: ReturnType<typeof setTimeout> | null = null;
const FLUSH_INTERVAL_MS = 5_000;
const MAX_BUFFER = 100;

async function flushLogs(): Promise<void> {
  if (logBuffer.length === 0) return;
  const lines = logBuffer;
  logBuffer = [];
  if (!WAREHOUSE_ID) return; // skip if not configured
  try {
    const token = await resolveToken();
    const host = resolveHost();
    const values = lines.map((l) => {
      const escaped = l.replace(/'/g, "''").slice(0, 1000);
      return `(current_timestamp(), '${APP_NAME}', '${CIPHER}', '${LANG}', '${escaped}')`;
    }).join(', ');
    const statement = `INSERT INTO ${LOG_TABLE} (ts, app_name, cipher_type, source_language, line) VALUES ${values}`;
    await fetch(host + '/api/2.0/sql/statements', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ warehouse_id: WAREHOUSE_ID, statement, wait_timeout: '10s' }),
    });
  } catch {
    // Log forwarding is best-effort; drop on failure rather than blocking the loop.
  }
}

function scheduleFlush(): void {
  if (flushTimer) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    void flushLogs();
  }, FLUSH_INTERVAL_MS);
}

// Tee console.log/warn/error into the buffered UC writer. Keeps stdout intact
// so Databricks Apps console logs continue to work normally.
const origLog = console.log.bind(console);
const origWarn = console.warn.bind(console);
const origErr = console.error.bind(console);
function teeLog(level: string, args: unknown[]): void {
  const line = `${level} ${args.map((a) => typeof a === 'string' ? a : JSON.stringify(a)).join(' ')}`;
  logBuffer.push(line);
  if (logBuffer.length >= MAX_BUFFER) void flushLogs();
  else scheduleFlush();
}
console.log = (...a: unknown[]) => { origLog(...a); teeLog('INFO', a); };
console.warn = (...a: unknown[]) => { origWarn(...a); teeLog('WARN', a); };
console.error = (...a: unknown[]) => { origErr(...a); teeLog('ERR', a); };

let bursted = 0;

// Health endpoint — Databricks Apps requires a process listening on PORT.
const app = express();
app.get('/health', (_req, res) => {
  res.json({ ok: true, app: APP_NAME, cipher: CIPHER, language: LANG, bursted: bursted });
});
app.get('/', (_req, res) => {
  res.type('text/plain').send(`voynich loop runner — cipher=${CIPHER} language=${LANG} bursted=${bursted}\n`);
});
const port = parseInt(process.env.PORT ?? '8000');
app.listen(port, () => {
  console.log(`[loop-runner] listening on :${port} cipher=${CIPHER} language=${LANG}`);
});

// Outer loop — runTheoryLoop terminates after numBursts; we restart it forever.
async function main(): Promise<void> {
  console.log(`[loop-runner] starting cipher=${CIPHER} language=${LANG} bursts_per_run=${BURSTS_PER_RUN}`);
  while (true) {
    try {
      await runTheoryLoop(BURSTS_PER_RUN, bursted);
      bursted += BURSTS_PER_RUN;
    } catch (err) {
      console.error(`[loop-runner] runTheoryLoop crashed:`, err);
      await new Promise((r) => setTimeout(r, 30_000));
    }
  }
}
void main();
