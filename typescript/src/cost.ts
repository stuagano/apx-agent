/**
 * Cost-tracking helpers — DBU / $ per agent from system tables.
 *
 * Databricks emits per-endpoint usage to `system.billing.usage` (DBU
 * consumption) and exposes per-resource pricing via
 * `system.billing.list_prices`. These helpers join the two to answer
 * "how much did agent X cost in the last 24h?" without each caller
 * re-deriving the query.
 *
 * The query shape (simplified):
 *
 *     SELECT
 *       sku_name,
 *       usage_unit,
 *       SUM(usage_quantity) AS dbus,
 *       SUM(usage_quantity * lp.pricing_default) AS usd
 *     FROM system.billing.usage u
 *     LEFT JOIN (latest pricing per sku) lp
 *       ON u.sku_name = lp.sku_name AND u.usage_unit = lp.usage_unit
 *     WHERE u.usage_metadata.endpoint_name = ?
 *       AND u.usage_start_time > CURRENT_TIMESTAMP - INTERVAL ? HOUR
 *
 * Pricing joins are best-effort — if the workspace hasn't enabled the
 * pricing share, `totalUsd` is `null`.
 *
 * @example
 * import { costForEndpoint } from 'apx-internal-runtime';
 *
 * const breakdown = await costForEndpoint({
 *   endpoint: 'customer_triage',
 *   sqlExecutor: (sql) => myWs.runSql(sql),
 *   lookbackHours: 24,
 * });
 * console.log(breakdown.totalDbus, breakdown.totalUsd);
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Raw row shape returned by the system-tables SQL. */
export interface CostRow {
  sku_name: string | null;
  usage_unit: string | null;
  dbus: number;
  usd: number | null;
  unit_price: number | null;
}

/** Caller-supplied SQL executor — takes a SQL string, returns rows. */
export type SqlExecutor = (sql: string) => Promise<Array<Record<string, unknown>>>;

/** Result type — per-SKU rows + totals for one lookup. */
export class CostBreakdown {
  readonly endpoint: string;
  readonly lookbackHours: number;
  readonly rows: ReadonlyArray<CostRow>;
  readonly totalDbus: number;
  readonly totalUsd: number | null;

  constructor(args: {
    endpoint: string;
    lookbackHours: number;
    rows?: ReadonlyArray<CostRow>;
    totalDbus?: number;
    totalUsd?: number | null;
  }) {
    this.endpoint = args.endpoint;
    this.lookbackHours = args.lookbackHours;
    this.rows = args.rows ?? [];
    this.totalDbus = args.totalDbus ?? 0;
    this.totalUsd = args.totalUsd ?? null;
  }

  /**
   * Aggregate raw rows into totals.
   *
   * `totalUsd` is `null` when ANY row is missing pricing — partial
   * coverage would understate the bill, so we surface null rather
   * than a misleading partial sum.
   */
  static fromRows(args: {
    endpoint: string;
    lookbackHours: number;
    rows: ReadonlyArray<CostRow>;
  }): CostBreakdown {
    const { endpoint, lookbackHours, rows } = args;
    const totalDbus = rows.reduce((acc, r) => acc + (Number(r.dbus) || 0), 0);

    let totalUsd: number | null;
    if (rows.length > 0 && rows.every((r) => typeof r.usd === 'number' && r.usd !== null)) {
      totalUsd = rows.reduce((acc, r) => acc + (r.usd as number), 0);
    } else {
      totalUsd = null;
    }

    return new CostBreakdown({
      endpoint,
      lookbackHours,
      rows: [...rows],
      totalDbus,
      totalUsd,
    });
  }
}

// ---------------------------------------------------------------------------
// SQL construction
// ---------------------------------------------------------------------------

/** Escape a string literal for inline SQL — single quotes doubled. */
function escapeSql(value: string): string {
  return value.replaceAll("'", "''");
}

/** Build the system-tables join query for a single endpoint window. */
export function buildCostSql(endpoint: string, lookbackHours: number): string {
  return (
    'SELECT ' +
    '  u.sku_name, ' +
    '  u.usage_unit, ' +
    '  SUM(u.usage_quantity) AS dbus, ' +
    '  SUM(u.usage_quantity * COALESCE(lp.pricing_default, 0)) AS usd, ' +
    '  MAX(lp.pricing_default) AS unit_price ' +
    'FROM system.billing.usage u ' +
    'LEFT JOIN ( ' +
    '  SELECT sku_name, usage_unit, ' +
    '         CAST(pricing.default AS DOUBLE) AS pricing_default ' +
    '  FROM system.billing.list_prices ' +
    '  WHERE price_end_time IS NULL ' +
    ') lp ON u.sku_name = lp.sku_name AND u.usage_unit = lp.usage_unit ' +
    `WHERE u.usage_metadata.endpoint_name = '${escapeSql(endpoint)}' ` +
    `  AND u.usage_start_time > CURRENT_TIMESTAMP - INTERVAL ${lookbackHours} HOUR ` +
    'GROUP BY u.sku_name, u.usage_unit ' +
    'ORDER BY dbus DESC'
  );
}

// ---------------------------------------------------------------------------
// Row normalisation
// ---------------------------------------------------------------------------

/**
 * Normalise a raw row. Zero/missing USD → null so callers can
 * distinguish "free SKU" from "unknown price".
 */
function normaliseRow(r: Record<string, unknown>): CostRow {
  const rawUsd = r['usd'];
  let usd: number | null = null;
  if (rawUsd !== null && rawUsd !== undefined) {
    const n = Number(rawUsd);
    if (Number.isFinite(n) && n > 0) {
      usd = n;
    }
  }

  const dbusRaw = r['dbus'];
  const dbus = dbusRaw === null || dbusRaw === undefined ? 0 : Number(dbusRaw) || 0;

  const unitPriceRaw = r['unit_price'];
  let unitPrice: number | null = null;
  if (unitPriceRaw !== null && unitPriceRaw !== undefined) {
    const n = Number(unitPriceRaw);
    if (Number.isFinite(n)) unitPrice = n;
  }

  return {
    sku_name: (r['sku_name'] as string | null | undefined) ?? null,
    usage_unit: (r['usage_unit'] as string | null | undefined) ?? null,
    dbus,
    usd,
    unit_price: unitPrice,
  };
}

// ---------------------------------------------------------------------------
// costForEndpoint
// ---------------------------------------------------------------------------

const CostForEndpointOptionsSchema = z.object({
  endpoint: z.string().min(1, 'endpoint must be a non-empty string'),
  sqlExecutor: z.custom<SqlExecutor>((v) => typeof v === 'function', {
    message: 'sqlExecutor must be a function',
  }),
  lookbackHours: z.number().int().positive('lookbackHours must be positive').default(24),
  warehouseId: z.string().optional(),
});

export interface CostForEndpointOptions {
  /** Serving endpoint name (the one `databricks.agents.deploy` produced). */
  endpoint: string;
  /** SQL executor — `(sql) => Promise<Row[]>`. Wires auth + warehouse. */
  sqlExecutor: SqlExecutor;
  /** Window to aggregate over. Default 24h. */
  lookbackHours?: number;
  /** Optional SQL warehouse ID — reserved for executor implementations that
   * accept one. Kept here so the API mirrors the Python signature; the
   * executor itself decides how to use it. */
  warehouseId?: string;
}

/**
 * Return DBU + USD breakdown for a Model Serving endpoint.
 *
 * `totalUsd` is `null` when pricing is unavailable (e.g. the
 * `system.billing.list_prices` share isn't enabled in this workspace).
 *
 * Degrades to an empty breakdown on SQL failure — never throws after
 * the SQL executor is invoked.
 */
export async function costForEndpoint(options: CostForEndpointOptions): Promise<CostBreakdown> {
  const parsed = CostForEndpointOptionsSchema.parse(options);
  const { endpoint, sqlExecutor, lookbackHours } = parsed;

  const sql = buildCostSql(endpoint, lookbackHours);

  let rawRows: Array<Record<string, unknown>>;
  try {
    rawRows = await sqlExecutor(sql);
  } catch (e) {
    console.warn(
      `costForEndpoint(${endpoint}): SQL failed: ${e instanceof Error ? e.message : String(e)} ` +
        '— returning empty breakdown. Common causes: system.billing.usage share ' +
        'not enabled, or insufficient grants on system schemas.',
    );
    return new CostBreakdown({ endpoint, lookbackHours });
  }

  const normalised = rawRows.map(normaliseRow);
  return CostBreakdown.fromRows({ endpoint, lookbackHours, rows: normalised });
}

// ---------------------------------------------------------------------------
// costForAgent
// ---------------------------------------------------------------------------

const CostForAgentOptionsSchema = z
  .object({
    agentName: z.string().min(1).optional(),
    endpoint: z.string().min(1).optional(),
    sqlExecutor: z.custom<SqlExecutor>((v) => typeof v === 'function', {
      message: 'sqlExecutor must be a function',
    }),
    lookbackHours: z.number().int().positive('lookbackHours must be positive').default(24),
    warehouseId: z.string().optional(),
  })
  .refine((d) => Boolean(d.agentName || d.endpoint), {
    message: 'Pass agentName or endpoint.',
  });

export interface CostForAgentOptions {
  /** Agent name. Used as the endpoint when `endpoint` is not given. */
  agentName?: string;
  /** Explicit serving endpoint. Wins over `agentName`. */
  endpoint?: string;
  /** SQL executor — `(sql) => Promise<Row[]>`. */
  sqlExecutor: SqlExecutor;
  /** Window to aggregate over. Default 24h. */
  lookbackHours?: number;
  /** Optional SQL warehouse ID. */
  warehouseId?: string;
}

/**
 * Resolve an agent name to its serving endpoint, then return cost.
 *
 * Either `agentName` or `endpoint` must be set. When only `agentName`
 * is provided, the endpoint is assumed to share the agent's name —
 * which matches the default `databricks.agents.deploy` behaviour.
 * Pass `endpoint` explicitly if they differ.
 */
export async function costForAgent(options: CostForAgentOptions): Promise<CostBreakdown> {
  const parsed = CostForAgentOptionsSchema.parse(options);
  const resolved = parsed.endpoint ?? parsed.agentName;
  // refine above guarantees one of the two is set
  if (!resolved) {
    throw new Error('Pass agentName or endpoint.');
  }
  return costForEndpoint({
    endpoint: resolved,
    sqlExecutor: parsed.sqlExecutor,
    lookbackHours: parsed.lookbackHours,
    warehouseId: parsed.warehouseId,
  });
}
