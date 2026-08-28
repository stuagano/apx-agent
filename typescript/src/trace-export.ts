/**
 * Trace exporter — dump MLflow traces to a Delta table for analytics.
 *
 * MLflow's traces table is great for ad-hoc browsing but awkward for
 * analytical queries downstream consumers want (cost-per-agent rollups,
 * tool-call frequency, latency P95 per operation, prompt heatmaps,
 * audit-event correlation across users). This exporter periodically
 * copies traces into a Delta table with a flat schema so it can be
 * joined to `system.billing.usage`, watchdog violation tables, etc.
 *
 * Schema (auto-created on first write when `autoCreate=true`):
 *
 *     CREATE TABLE IF NOT EXISTS {target_table} (
 *         trace_id        STRING NOT NULL,
 *         experiment_id   STRING,
 *         agent_name      STRING,
 *         operation       STRING,
 *         status          STRING,
 *         start_time_ms   BIGINT,
 *         execution_time_ms BIGINT,
 *         session_id      STRING,
 *         user_token_provided BOOLEAN,
 *         model_endpoint  STRING,
 *         tool_count      INT,
 *         watchdog_action STRING,
 *         watchdog_policy_id STRING,
 *         tags            STRING,
 *         exported_at     TIMESTAMP
 *     ) USING DELTA
 *
 * The TypeScript port lacks an MLflow SDK, so traces are supplied via
 * a caller-injected `traceSearch` callable; SQL writes go through a
 * caller-injected `sqlExecutor`. Both let consumers wire whatever
 * REST/SDK plumbing they prefer (Databricks SQL Statements API,
 * a mock, an alternate warehouse driver, etc.).
 *
 * @example
 * import { exportTraces } from 'apx-internal-runtime';
 *
 * const result = await exportTraces({
 *   experimentName: '/my/exp',
 *   targetTable: 'main.obs.agent_traces',
 *   traceSearch: async () => mlflowFetchTraces(),
 *   sqlExecutor: async (sql) => myWs.runSql(sql),
 * });
 * console.log(result.rowsWritten);
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Tag/attribute bag from a trace — strings, numbers, bools, anything. */
export type TraceAttrs = Record<string, unknown>;

/** DataFrame-row shape OR Trace-object shape — `_normaliseTrace` handles both. */
export type RawTrace = Record<string, unknown> | TraceLike;

/** Trace-object shape (mirrors MLflow's `Trace`). */
export interface TraceLike {
  info?: TraceInfoLike | null;
  data?: TraceDataLike | null;
}

export interface TraceInfoLike {
  trace_id?: string | null;
  request_id?: string | null;
  experiment_id?: string | null;
  status?: string | null;
  timestamp_ms?: number | null;
  execution_time_ms?: number | null;
  tags?: TraceAttrs | null;
}

export interface TraceDataLike {
  spans?: ReadonlyArray<{ attributes?: TraceAttrs | null }> | null;
}

/** Caller-supplied trace search — wires MLflow / mocks / replays. */
export type TraceSearch = (opts: {
  experimentNames: string[];
  maxResults: number;
}) => Promise<RawTrace[]>;

/** Caller-supplied SQL executor — single statement, no row return required. */
export type SqlExecutor = (sql: string) => Promise<void>;

/** Normalised row written to the Delta target. */
export interface NormalisedTrace {
  trace_id: string;
  experiment_id: string;
  agent_name: string | null;
  operation: string | null;
  status: string;
  start_time_ms: number | null;
  execution_time_ms: number | null;
  session_id: string | null;
  user_token_provided: boolean | null;
  model_endpoint: string | null;
  tool_count: number | null;
  watchdog_action: string | null;
  watchdog_policy_id: string | null;
  tags: string;
}

/** Summary of one export run — frozen. */
export interface ExportResult {
  readonly targetTable: string;
  readonly tracesPulled: number;
  readonly rowsWritten: number;
  readonly skipped: number;
}

function makeExportResult(args: {
  targetTable: string;
  tracesPulled: number;
  rowsWritten: number;
  skipped: number;
}): ExportResult {
  return Object.freeze({
    targetTable: args.targetTable,
    tracesPulled: args.tracesPulled,
    rowsWritten: args.rowsWritten,
    skipped: args.skipped,
  });
}

// ---------------------------------------------------------------------------
// Coercion helpers
// ---------------------------------------------------------------------------

function coerceInt(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number' && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  if (typeof value === 'bigint') {
    return Number(value);
  }
  if (typeof value === 'string') {
    const n = Number(value);
    if (Number.isFinite(n)) return Math.trunc(n);
    return null;
  }
  if (typeof value === 'boolean') {
    return value ? 1 : 0;
  }
  return null;
}

function coerceBool(value: unknown): boolean | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    return ['true', '1', 'yes'].includes(value.toLowerCase());
  }
  if (typeof value === 'number') return Boolean(value);
  return Boolean(value);
}

function coerceStringOrNull(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') return value;
  return String(value);
}

// ---------------------------------------------------------------------------
// _normaliseTrace
// ---------------------------------------------------------------------------

function isPlainDictRow(t: RawTrace): t is Record<string, unknown> {
  if (t === null || typeof t !== 'object') return false;
  // Trace-object shape exposes `info` (even when null). Treat anything with
  // an `info` key as the object shape; everything else falls into the
  // DataFrame-row branch. Matches the Python isinstance(trace, dict) check
  // (DataFrames' `to_dict(orient='records')` yields plain dicts without
  // an `info` field).
  if ('info' in t) return false;
  return true;
}

function jsonStringifyApx(attrs: TraceAttrs): string {
  const apxOnly: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('apx.')) {
      // Mirror Python's json.dumps(..., default=str): non-JSON-safe values
      // serialise via their string form.
      apxOnly[k] = serialisableValue(v);
    }
  }
  return JSON.stringify(apxOnly);
}

function serialisableValue(v: unknown): unknown {
  if (v === null || v === undefined) return v;
  const t = typeof v;
  if (t === 'string' || t === 'number' || t === 'boolean') return v;
  if (t === 'bigint') return String(v);
  // Arrays / plain objects are serialisable.
  if (Array.isArray(v)) return v.map(serialisableValue);
  if (t === 'object') {
    // Plain object → keep as-is (JSON.stringify can handle it). Fall back to
    // String() for class instances / exotic shapes to match Python `default=str`.
    if (Object.getPrototypeOf(v) === Object.prototype || Object.getPrototypeOf(v) === null) {
      const out: Record<string, unknown> = {};
      for (const [k, vv] of Object.entries(v as Record<string, unknown>)) {
        out[k] = serialisableValue(vv);
      }
      return out;
    }
    return String(v);
  }
  return String(v);
}

/**
 * Pull the apx.* attributes off a trace into a flat row.
 *
 * Returns `null` when the trace has no usable id (caller increments
 * `skipped`). Mirrors the Python implementation: DataFrame rows pull
 * attrs from `tags`/`attributes`; Trace objects merge `info.tags` with
 * the root span's attributes, with span attrs taking precedence.
 */
export function _normaliseTrace(trace: RawTrace): NormalisedTrace | null {
  if (trace === null || trace === undefined) return null;

  // DataFrame row (dict-like)
  if (isPlainDictRow(trace)) {
    const row = trace;
    const rawAttrs = (row['tags'] ?? row['attributes']) as TraceAttrs | undefined | null;
    const attrs: TraceAttrs = rawAttrs && typeof rawAttrs === 'object' ? rawAttrs : {};
    const traceId = row['trace_id'] ?? row['request_id'];
    if (traceId === null || traceId === undefined || traceId === '') return null;
    return {
      trace_id: String(traceId),
      experiment_id: String(row['experiment_id'] ?? ''),
      agent_name: coerceStringOrNull(attrs['apx.agent.name']),
      operation: coerceStringOrNull(attrs['apx.operation']),
      status: String(row['status'] ?? ''),
      start_time_ms: coerceInt(row['timestamp_ms'] ?? row['start_time_ms']),
      execution_time_ms: coerceInt(row['execution_time_ms']),
      session_id: coerceStringOrNull(attrs['apx.session.id']),
      user_token_provided: coerceBool(attrs['apx.user.token_provided']),
      model_endpoint: coerceStringOrNull(attrs['apx.model.endpoint']),
      tool_count: coerceInt(attrs['apx.tools.count']),
      watchdog_action: coerceStringOrNull(attrs['apx.watchdog.action']),
      watchdog_policy_id: coerceStringOrNull(attrs['apx.watchdog.policy_id']),
      tags: jsonStringifyApx(attrs),
    };
  }

  // Trace-object case
  const traceObj = trace as TraceLike;
  const info = traceObj.info;
  if (info === null || info === undefined) return null;
  const traceId = info.trace_id ?? info.request_id;
  if (traceId === null || traceId === undefined || traceId === '') return null;

  const spans = (traceObj.data?.spans ?? []) as ReadonlyArray<{
    attributes?: TraceAttrs | null;
  }>;
  let rootAttrs: TraceAttrs = {};
  if (spans.length > 0) {
    const a = spans[0]?.attributes;
    rootAttrs = a && typeof a === 'object' ? { ...a } : {};
  }
  const tagsRaw = info.tags;
  const tags: TraceAttrs = tagsRaw && typeof tagsRaw === 'object' ? { ...tagsRaw } : {};
  const attrs: TraceAttrs = { ...tags, ...rootAttrs }; // span attrs win

  return {
    trace_id: String(traceId),
    experiment_id: String(info.experiment_id ?? ''),
    agent_name: coerceStringOrNull(attrs['apx.agent.name']),
    operation: coerceStringOrNull(attrs['apx.operation']),
    status: String(info.status ?? ''),
    start_time_ms: coerceInt(info.timestamp_ms),
    execution_time_ms: coerceInt(info.execution_time_ms),
    session_id: coerceStringOrNull(attrs['apx.session.id']),
    user_token_provided: coerceBool(attrs['apx.user.token_provided']),
    model_endpoint: coerceStringOrNull(attrs['apx.model.endpoint']),
    tool_count: coerceInt(attrs['apx.tools.count']),
    watchdog_action: coerceStringOrNull(attrs['apx.watchdog.action']),
    watchdog_policy_id: coerceStringOrNull(attrs['apx.watchdog.policy_id']),
    tags: jsonStringifyApx(attrs),
  };
}

// ---------------------------------------------------------------------------
// SQL helpers
// ---------------------------------------------------------------------------

/**
 * Escape a string literal for inline SQL.
 *
 * Returns `'NULL'` for null/undefined so callers can inline the result
 * directly. Single quotes inside the value are doubled.
 */
export function escapeSql(value: string | null | undefined): string {
  if (value === null || value === undefined) return 'NULL';
  return "'" + String(value).replaceAll("'", "''") + "'";
}

function buildCreateTableDdl(targetTable: string): string {
  return (
    `CREATE TABLE IF NOT EXISTS ${targetTable} (` +
    '  trace_id STRING NOT NULL,' +
    '  experiment_id STRING,' +
    '  agent_name STRING,' +
    '  operation STRING,' +
    '  status STRING,' +
    '  start_time_ms BIGINT,' +
    '  execution_time_ms BIGINT,' +
    '  session_id STRING,' +
    '  user_token_provided BOOLEAN,' +
    '  model_endpoint STRING,' +
    '  tool_count INT,' +
    '  watchdog_action STRING,' +
    '  watchdog_policy_id STRING,' +
    '  tags STRING,' +
    '  exported_at TIMESTAMP' +
    ') USING DELTA'
  );
}

function buildMergeSql(targetTable: string, rows: NormalisedTrace[], nowSeconds: number): string {
  const valueTuples = rows.map((r) => {
    const tokens: string[] = [
      escapeSql(r.trace_id),
      escapeSql(r.experiment_id),
      escapeSql(r.agent_name),
      escapeSql(r.operation),
      escapeSql(r.status),
      r.start_time_ms !== null ? String(r.start_time_ms) : 'NULL',
      r.execution_time_ms !== null ? String(r.execution_time_ms) : 'NULL',
      escapeSql(r.session_id),
      r.user_token_provided === null
        ? 'NULL'
        : r.user_token_provided
          ? 'TRUE'
          : 'FALSE',
      escapeSql(r.model_endpoint),
      r.tool_count !== null ? String(r.tool_count) : 'NULL',
      escapeSql(r.watchdog_action),
      escapeSql(r.watchdog_policy_id),
      escapeSql(r.tags),
      `CAST(${nowSeconds} AS TIMESTAMP)`,
    ];
    return '(' + tokens.join(', ') + ')';
  });

  return (
    `MERGE INTO ${targetTable} AS target ` +
    'USING (SELECT * FROM VALUES ' +
    valueTuples.join(', ') +
    ' AS src(' +
    '  trace_id, experiment_id, agent_name, operation, status,' +
    '  start_time_ms, execution_time_ms, session_id, user_token_provided,' +
    '  model_endpoint, tool_count, watchdog_action, watchdog_policy_id,' +
    '  tags, exported_at' +
    ')) src ' +
    'ON target.trace_id = src.trace_id ' +
    'WHEN MATCHED THEN UPDATE SET * ' +
    'WHEN NOT MATCHED THEN INSERT *'
  );
}

// ---------------------------------------------------------------------------
// exportTraces
// ---------------------------------------------------------------------------

const ExportTracesOptionsSchema = z.object({
  experimentName: z.string().min(1, 'experimentName must be a non-empty string'),
  targetTable: z.string().min(1, 'targetTable must be a non-empty string'),
  traceSearch: z.custom<TraceSearch>((v) => typeof v === 'function', {
    message: 'traceSearch must be a function',
  }),
  sqlExecutor: z.custom<SqlExecutor>((v) => typeof v === 'function', {
    message: 'sqlExecutor must be a function',
  }),
  lookbackHours: z.number().int().positive('lookbackHours must be positive').default(24),
  maxTraces: z.number().int().positive('maxTraces must be positive').default(1000),
  autoCreate: z.boolean().default(true),
  warehouseId: z.string().optional(),
});

export interface ExportTracesOptions {
  /** MLflow experiment name/id to read traces from. */
  experimentName: string;
  /** Three-part UC name (catalog.schema.table) for the destination Delta table. */
  targetTable: string;
  /** Caller-supplied trace search — wires MLflow / mocks / replays. */
  traceSearch: TraceSearch;
  /** Caller-supplied SQL executor — runs DDL + MERGE. */
  sqlExecutor: SqlExecutor;
  /** How far back to fetch. Default 24h. Kept for parity with Python signature;
   * caller's `traceSearch` decides how to apply it. */
  lookbackHours?: number;
  /** Cap on traces fetched per call. Default 1000. */
  maxTraces?: number;
  /** When true (default), runs `CREATE TABLE IF NOT EXISTS` before MERGE. */
  autoCreate?: boolean;
  /** Optional SQL warehouse ID — caller's `sqlExecutor` decides how to use it. */
  warehouseId?: string;
}

/**
 * Pull MLflow traces and write them to a Delta table.
 *
 * Rejects when `targetTable` is not a three-part UC name. Tolerates
 * DDL failure with a warning. MERGE failures degrade to
 * `rowsWritten: 0` — never throws after the SQL executor is invoked.
 */
export async function exportTraces(options: ExportTracesOptions): Promise<ExportResult> {
  const parsed = ExportTracesOptionsSchema.parse(options);
  const {
    experimentName,
    targetTable,
    traceSearch,
    sqlExecutor,
    maxTraces,
    autoCreate,
  } = parsed;

  // Validate three-part UC name (catalog.schema.table).
  const dotCount = (targetTable.match(/\./g) ?? []).length;
  if (dotCount !== 2) {
    throw new Error(
      `targetTable must be a three-part UC name; got ${JSON.stringify(targetTable)}`,
    );
  }

  if (autoCreate) {
    const ddl = buildCreateTableDdl(targetTable);
    try {
      await sqlExecutor(ddl);
    } catch (e) {
      console.warn(
        `exportTraces: CREATE TABLE IF NOT EXISTS ${targetTable} failed: ` +
          `${e instanceof Error ? e.message : String(e)} — ` +
          'subsequent inserts may fail.',
      );
    }
  }

  let rawTraces: RawTrace[];
  try {
    rawTraces = await traceSearch({
      experimentNames: [experimentName],
      maxResults: maxTraces,
    });
  } catch (e) {
    throw new Error(
      `traceSearch failed: ${e instanceof Error ? e.message : String(e)}`,
    );
  }

  const rows: NormalisedTrace[] = [];
  let skipped = 0;
  for (const t of rawTraces ?? []) {
    const norm = _normaliseTrace(t);
    if (norm === null) {
      skipped += 1;
      continue;
    }
    rows.push(norm);
  }

  if (rows.length === 0) {
    return makeExportResult({
      targetTable,
      tracesPulled: skipped,
      rowsWritten: 0,
      skipped,
    });
  }

  const nowSeconds = Date.now() / 1000;
  const sql = buildMergeSql(targetTable, rows, nowSeconds);

  let rowsWritten = 0;
  try {
    await sqlExecutor(sql);
    rowsWritten = rows.length;
  } catch (e) {
    console.warn(
      `exportTraces: MERGE INTO ${targetTable} failed: ` +
        `${e instanceof Error ? e.message : String(e)} — no rows written.`,
    );
    rowsWritten = 0;
  }

  return makeExportResult({
    targetTable,
    tracesPulled: rows.length + skipped,
    rowsWritten,
    skipped,
  });
}
