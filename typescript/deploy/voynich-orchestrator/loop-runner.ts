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

// Convergence: stop the search if the all-time best for this strategy hasn't
// improved in CONVERGENCE_BURSTS consecutive bursts. The /health endpoint
// stays up but the inner runTheoryLoop call is skipped after convergence.
const CONVERGENCE_BURSTS = parseInt(process.env.CONVERGENCE_BURSTS ?? '20');
let bursted = 0;
let allTimeBest = 0;
let staleBursts = 0;
let converged = false;

// Baseline reset: when the scoring formula changes meaningfully, set
// SCORING_BASELINE_AFTER to an ISO timestamp so the runner only considers
// theories proposed after that point as its all-time best. Prevents stale
// inflated peaks (from older scoring formulas / known pathological maps)
// from anchoring the convergence-detector.
const BASELINE_AFTER = process.env.SCORING_BASELINE_AFTER ?? '';

async function readAllTimeBest(): Promise<number> {
  if (!WAREHOUSE_ID) return 0;
  try {
    const token = await resolveToken();
    const host = resolveHost();
    const afterClause = BASELINE_AFTER
      ? ` AND proposed_at >= TIMESTAMP '${BASELINE_AFTER}'`
      : '';
    const r = await fetch(host + '/api/2.0/sql/statements', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        warehouse_id: WAREHOUSE_ID,
        statement: `SELECT COALESCE(MAX(grounding_score+consistency_score), 0) AS best
                    FROM serverless_stable_qh44kx_catalog.voynich.theories
                    WHERE cipher_type='${CIPHER}' AND source_language='${LANG}'${afterClause}`,
        wait_timeout: '10s',
      }),
    });
    const d = await r.json() as { result?: { data_array?: string[][] } };
    const val = d.result?.data_array?.[0]?.[0];
    return val ? parseFloat(val) : 0;
  } catch {
    return 0;
  }
}

// Health endpoint — Databricks Apps requires a process listening on PORT.
const app = express();
app.get('/health', (_req, res) => {
  res.json({
    ok: true, app: APP_NAME, cipher: CIPHER, language: LANG,
    bursted, allTimeBest, staleBursts, convergenceBursts: CONVERGENCE_BURSTS, converged,
  });
});
app.get('/', (_req, res) => {
  const statusLine = converged
    ? `CONVERGED — no improvement in ${staleBursts} bursts, best=${allTimeBest.toFixed(3)}`
    : `running — bursted=${bursted} best=${allTimeBest.toFixed(3)} stale=${staleBursts}/${CONVERGENCE_BURSTS}`;
  res.type('text/plain').send(`voynich loop runner — cipher=${CIPHER} language=${LANG}\n${statusLine}\n`);
});
const port = parseInt(process.env.PORT ?? '8000');
app.listen(port, () => {
  console.log(`[loop-runner] listening on :${port} cipher=${CIPHER} language=${LANG}`);
});

// Outer loop — runTheoryLoop terminates after numBursts; we restart it until
// the strategy converges (no improvement in CONVERGENCE_BURSTS bursts), then
// idle while keeping /health responsive.
async function main(): Promise<void> {
  console.log(`[loop-runner] starting cipher=${CIPHER} language=${LANG} bursts_per_run=${BURSTS_PER_RUN} convergence_bursts=${CONVERGENCE_BURSTS}`);
  allTimeBest = await readAllTimeBest();
  console.log(`[loop-runner] starting all-time best for this strategy: ${allTimeBest.toFixed(3)}`);

  while (!converged) {
    try {
      const theories = await runTheoryLoop(BURSTS_PER_RUN, bursted);
      bursted += BURSTS_PER_RUN;
      const runMax = theories.reduce((m, t) => Math.max(m, t.grounding_score + t.consistency_score), 0);
      if (runMax > allTimeBest) {
        console.log(`[loop-runner] new all-time best: ${runMax.toFixed(3)} (was ${allTimeBest.toFixed(3)}) — stale counter reset`);
        allTimeBest = runMax;
        staleBursts = 0;
      } else {
        staleBursts += BURSTS_PER_RUN;
        console.log(`[loop-runner] no improvement after burst (best=${allTimeBest.toFixed(3)}, run_max=${runMax.toFixed(3)}) stale=${staleBursts}/${CONVERGENCE_BURSTS}`);
        if (staleBursts >= CONVERGENCE_BURSTS) {
          console.log(`[loop-runner] CONVERGED — ${staleBursts} bursts without improvement on cipher=${CIPHER} lang=${LANG}, best=${allTimeBest.toFixed(3)}. Halting theory loop.`);
          converged = true;
        }
      }
    } catch (err) {
      console.error(`[loop-runner] runTheoryLoop crashed:`, err);
      await new Promise((r) => setTimeout(r, 30_000));
    }
  }

  // Converged — keep /health alive so the App stays RUNNING; the dashboard's
  // "latest" age column will surface staleness for the operator.
  while (true) {
    await new Promise((r) => setTimeout(r, 60_000));
  }
}
void main();
