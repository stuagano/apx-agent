/**
 * Canary / A-B deployment helpers — multi-version serving with traffic split.
 *
 * Databricks Model Serving lets one endpoint serve multiple model versions
 * concurrently with a configurable traffic split. The platform primitives
 * (`ws.serving_endpoints.update_config` + `TrafficConfig` + `Route`) are
 * the right surface; what's missing is the *workflow*:
 *
 *   * `deployCanary({endpoint, registeredModelName, newVersion, canaryTrafficPct=10, ...})` —
 *     Add `newVersion` as a second served entity. Allocate `canaryTrafficPct`
 *     to it; redistribute the rest across existing served entities
 *     proportionally to their current split.
 *   * `promoteCanary({endpoint, registeredModelName, version, ...})` —
 *     Send 100% of traffic to `version`. Other entities stay configured.
 *   * `rollbackCanary` — alias for promoteCanary, named for semantic clarity.
 *   * `analyzeCanary({endpoint, experiment, lookbackHours=24, traceSearch, ...})` —
 *     Partitions traces by served-entity version and reports per-version
 *     request counts + latency P50/P95/avg + error counts.
 *
 * Per-version trace correlation depends on each served entity having a
 * distinct served-entity `name` (the SDK names them `<short_model>-<version>`).
 *
 * ## ServingEndpointsClient interface
 *
 * The Databricks JS SDK doesn't expose `update_config`/`TrafficConfig` in a
 * convenient typed wrapper, so callers inject a client matching this shape:
 *
 * ```ts
 * interface ServingEndpointsClient {
 *   get(name: string): Promise<EndpointInfo>;
 *   updateConfig(name: string, config: {
 *     servedEntities?: ServedEntityInput[];
 *     trafficConfig?: { routes: Route[] };
 *   }): Promise<void>;
 * }
 * ```
 *
 * Where `EndpointInfo` exposes either `config` or `pendingConfig` with
 * `servedEntities` (or legacy `servedModels`) and `trafficConfig.routes`.
 *
 * ## traceSearch
 *
 * `analyzeCanary` accepts an injected `traceSearch` callable that returns a
 * Promise of trace rows. This keeps the MLflow SDK call abstracted — the
 * caller wires in `mlflow.search_traces` or any other backing source.
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A served entity entry: tuple of (name, entityName, entityVersion). */
export interface ServedEntityRecord {
  name: string;
  entityName: string;
  entityVersion: string;
}

/** Snapshot of an endpoint's current canary state. */
export interface CanaryConfig {
  endpoint: string;
  servedEntities: ServedEntityRecord[];
  trafficSplit: Record<string, number>;
}

/** Input shape for ServedEntityInput on updateConfig (mirrors SDK). */
export interface ServedEntityInput {
  name: string;
  entityName: string | null;
  entityVersion: string | null;
  scaleToZeroEnabled: boolean;
  workloadSize: string;
}

/** Route entry inside trafficConfig.routes. */
export interface Route {
  servedEntityName: string;
  trafficPercentage: number;
}

/** Per-version aggregate metrics class with errorRate getter. */
export class VersionMetrics {
  readonly version: string;
  readonly requests: number;
  readonly errors: number;
  readonly latencyP50Ms: number | null;
  readonly latencyP95Ms: number | null;
  readonly latencyAvgMs: number | null;

  constructor(opts: {
    version: string;
    requests: number;
    errors: number;
    latencyP50Ms: number | null;
    latencyP95Ms: number | null;
    latencyAvgMs: number | null;
  }) {
    this.version = opts.version;
    this.requests = opts.requests;
    this.errors = opts.errors;
    this.latencyP50Ms = opts.latencyP50Ms;
    this.latencyP95Ms = opts.latencyP95Ms;
    this.latencyAvgMs = opts.latencyAvgMs;
  }

  get errorRate(): number {
    return this.requests ? this.errors / this.requests : 0.0;
  }
}

/** Per-version comparison across a lookback window. */
export class CanaryReport {
  readonly endpoint: string;
  readonly lookbackHours: number;
  readonly versions: VersionMetrics[];

  constructor(opts: {
    endpoint: string;
    lookbackHours: number;
    versions?: VersionMetrics[];
  }) {
    this.endpoint = opts.endpoint;
    this.lookbackHours = opts.lookbackHours;
    this.versions = opts.versions ?? [];
  }

  /** Version with the lowest P95 latency (tie-broken by higher request count). */
  bestByLatency(): VersionMetrics | null {
    const eligible = this.versions.filter((v) => v.latencyP95Ms !== null);
    if (eligible.length === 0) return null;
    return eligible.reduce((best, v) => {
      const bp95 = best.latencyP95Ms ?? 0;
      const vp95 = v.latencyP95Ms ?? 0;
      if (vp95 < bp95) return v;
      if (vp95 > bp95) return best;
      // Tie-break: prefer higher request count (min by -requests in Python)
      return v.requests > best.requests ? v : best;
    });
  }

  /** Version with the lowest error rate (tie-broken by P95 latency). */
  bestByErrorRate(): VersionMetrics | null {
    const eligible = this.versions.filter((v) => v.requests > 0);
    if (eligible.length === 0) return null;
    return eligible.reduce((best, v) => {
      if (v.errorRate < best.errorRate) return v;
      if (v.errorRate > best.errorRate) return best;
      const bp95 = best.latencyP95Ms ?? 0;
      const vp95 = v.latencyP95Ms ?? 0;
      return vp95 < bp95 ? v : best;
    });
  }
}

// ---------------------------------------------------------------------------
// ServingEndpointsClient + TraceSearch interfaces
// ---------------------------------------------------------------------------

/** SDK response shapes — duck-typed via field access. */
export interface ServedEntityResponse {
  name?: string | null;
  entityName?: string | null;
  entityVersion?: string | null;
  // Legacy shape:
  modelName?: string | null;
  modelVersion?: string | null;
}

export interface RouteResponse {
  servedEntityName?: string | null;
  servedModelName?: string | null;
  trafficPercentage?: number | null;
}

export interface EndpointConfigResponse {
  servedEntities?: ServedEntityResponse[] | null;
  servedModels?: ServedEntityResponse[] | null;
  trafficConfig?: { routes?: RouteResponse[] | null } | null;
}

export interface EndpointInfo {
  config?: EndpointConfigResponse | null;
  pendingConfig?: EndpointConfigResponse | null;
}

export interface UpdateConfigPayload {
  servedEntities?: ServedEntityInput[];
  trafficConfig?: { routes: Route[] };
}

export interface ServingEndpointsClient {
  get(name: string): Promise<EndpointInfo>;
  updateConfig(name: string, config: UpdateConfigPayload): Promise<void>;
}

/** Raw trace row — minimal shape consumed by analyzeCanary. */
export interface TraceRow {
  tags?: Record<string, unknown> | null;
  attributes?: Record<string, unknown> | null;
  status?: string | null;
  execution_time_ms?: number | string | null;
  info?: {
    tags?: Record<string, unknown> | null;
    status?: string | null;
    execution_time_ms?: number | string | null;
  } | null;
}

export type TraceSearch = (opts: {
  experiment: string;
  maxTraces: number;
}) => Promise<TraceRow[]>;

// ---------------------------------------------------------------------------
// Endpoint config inspection
// ---------------------------------------------------------------------------

/** Build the served-entity name the SDK uses for (model, version) tuples. */
export function _entityName(modelFullName: string, version: number | string): string {
  const parts = modelFullName.split('.');
  const short = parts[parts.length - 1];
  return `${short}-${version}`;
}

const GetCanaryConfigOpts = z.object({
  endpoint: z.string().min(1),
  servingEndpointsClient: z.custom<ServingEndpointsClient>(
    (v) => typeof v === 'object' && v !== null && 'get' in v && 'updateConfig' in v,
    'servingEndpointsClient must implement get/updateConfig',
  ),
});

/**
 * Read the endpoint's current served entities + traffic split.
 *
 * Returns a `CanaryConfig` snapshot. Callers use it to inspect state, build
 * modified configs for `updateConfig` calls, or surface in CLI output.
 */
export async function getCanaryConfig(opts: {
  endpoint: string;
  servingEndpointsClient: ServingEndpointsClient;
}): Promise<CanaryConfig> {
  const { endpoint, servingEndpointsClient } = GetCanaryConfigOpts.parse(opts);

  const info = await servingEndpointsClient.get(endpoint);
  const config = info?.config ?? info?.pendingConfig ?? null;
  if (config == null) {
    return { endpoint, servedEntities: [], trafficSplit: {} };
  }

  const served: ServedEntityResponse[] =
    config.servedEntities ?? config.servedModels ?? [];

  const entries: ServedEntityRecord[] = served.map((s) => ({
    name: (s?.name ?? '') || '',
    entityName: (s?.entityName ?? s?.modelName ?? '') || '',
    entityVersion: String(s?.entityVersion ?? s?.modelVersion ?? ''),
  }));

  const split: Record<string, number> = {};
  const routes = config.trafficConfig?.routes ?? [];
  for (const route of routes) {
    const key = route?.servedEntityName ?? route?.servedModelName ?? '';
    if (key) {
      const pct = route?.trafficPercentage ?? 0;
      split[key] = Math.trunc(Number(pct) || 0);
    }
  }

  return { endpoint, servedEntities: entries, trafficSplit: split };
}

// ---------------------------------------------------------------------------
// Deploy / promote / rollback
// ---------------------------------------------------------------------------

const DeployCanaryOpts = z.object({
  endpoint: z.string().min(1),
  registeredModelName: z.string().min(1),
  newVersion: z.union([z.number(), z.string()]),
  canaryTrafficPct: z.number().int().default(10),
  servingEndpointsClient: z.custom<ServingEndpointsClient>(
    (v) => typeof v === 'object' && v !== null && 'get' in v && 'updateConfig' in v,
  ),
  scaleToZeroEnabled: z.boolean().default(true),
  workloadSize: z.string().default('Small'),
});

/**
 * Add `newVersion` as a canary served entity on `endpoint`.
 *
 * Allocates `canaryTrafficPct` to the new version and redistributes the
 * remainder across the existing served entities, weighted by their *current*
 * traffic share (so a 70/30 split becomes 70*0.9 / 30*0.9 / new at 10).
 */
export async function deployCanary(opts: {
  endpoint: string;
  registeredModelName: string;
  newVersion: number | string;
  canaryTrafficPct?: number;
  servingEndpointsClient: ServingEndpointsClient;
  scaleToZeroEnabled?: boolean;
  workloadSize?: string;
}): Promise<CanaryConfig> {
  const parsed = DeployCanaryOpts.parse(opts);
  const {
    endpoint,
    registeredModelName,
    newVersion,
    canaryTrafficPct,
    servingEndpointsClient,
    scaleToZeroEnabled,
    workloadSize,
  } = parsed;

  if (canaryTrafficPct < 1 || canaryTrafficPct > 99) {
    throw new Error(
      `canaryTrafficPct must be in [1, 99]; got ${canaryTrafficPct}`,
    );
  }

  const current = await getCanaryConfig({ endpoint, servingEndpointsClient });
  const newEntityName = _entityName(registeredModelName, newVersion);

  // Build the new served-entities list = existing + canary.
  const servedInputs: ServedEntityInput[] = [];
  const seen = new Set<string>();
  for (const rec of current.servedEntities) {
    if (!rec.name || rec.name === newEntityName) continue;
    seen.add(rec.name);
    servedInputs.push({
      name: rec.name,
      entityName: rec.entityName || null,
      entityVersion: rec.entityVersion || null,
      scaleToZeroEnabled,
      workloadSize,
    });
  }
  servedInputs.push({
    name: newEntityName,
    entityName: registeredModelName,
    entityVersion: String(newVersion),
    scaleToZeroEnabled,
    workloadSize,
  });

  // Redistribute traffic — existing entities share (100 - canaryTrafficPct),
  // weighted by their current allocation; canary gets canaryTrafficPct.
  const remaining = 100 - canaryTrafficPct;
  const existingSplit: Record<string, number> = {};
  for (const [k, v] of Object.entries(current.trafficSplit)) {
    if (seen.has(k)) existingSplit[k] = v;
  }
  const totalExisting = Object.values(existingSplit).reduce((a, b) => a + b, 0);

  const routes: Route[] = [];
  let allocated = 0;
  if (totalExisting > 0 && seen.size > 0) {
    const scaled: Array<[string, number]> = [];
    for (const name of seen) {
      const share = (existingSplit[name] ?? 0) / totalExisting;
      const pct = pythonRound(share * remaining);
      scaled.push([name, pct]);
      allocated += pct;
    }
    // Reconcile rounding drift back into the largest existing entity
    if (scaled.length > 0 && allocated !== remaining) {
      scaled.sort((a, b) => b[1] - a[1]);
      const [topName, topPct] = scaled[0];
      scaled[0] = [topName, topPct + (remaining - allocated)];
    }
    for (const [name, pct] of scaled) {
      routes.push({ servedEntityName: name, trafficPercentage: pct });
    }
  } else if (seen.size > 0) {
    // No prior split (newly created endpoint?) — distribute evenly.
    const seenList = [...seen].sort();
    const even = Math.trunc(remaining / seenList.length);
    const extra = remaining - even * seenList.length;
    for (let i = 0; i < seenList.length; i++) {
      routes.push({
        servedEntityName: seenList[i],
        trafficPercentage: even + (i === 0 ? extra : 0),
      });
    }
  }
  routes.push({
    servedEntityName: newEntityName,
    trafficPercentage: canaryTrafficPct,
  });

  await servingEndpointsClient.updateConfig(endpoint, {
    servedEntities: servedInputs,
    trafficConfig: { routes },
  });

  return getCanaryConfig({ endpoint, servingEndpointsClient });
}

const PromoteCanaryOpts = z.object({
  endpoint: z.string().min(1),
  registeredModelName: z.string().min(1),
  version: z.union([z.number(), z.string()]),
  servingEndpointsClient: z.custom<ServingEndpointsClient>(
    (v) => typeof v === 'object' && v !== null && 'get' in v && 'updateConfig' in v,
  ),
});

/**
 * Send 100% of traffic to `version`. Other entities stay configured.
 *
 * Keeping the older served entities in the config (just at 0% traffic) means
 * `rollbackCanary` can swap traffic back without re-adding them.
 */
export async function promoteCanary(opts: {
  endpoint: string;
  registeredModelName: string;
  version: number | string;
  servingEndpointsClient: ServingEndpointsClient;
}): Promise<CanaryConfig> {
  const { endpoint, registeredModelName, version, servingEndpointsClient } =
    PromoteCanaryOpts.parse(opts);

  const target = _entityName(registeredModelName, version);
  const current = await getCanaryConfig({ endpoint, servingEndpointsClient });
  const servedNames = new Set(
    current.servedEntities.map((s) => s.name).filter((n) => !!n),
  );

  if (!servedNames.has(target)) {
    const available = [...servedNames].sort();
    throw new Error(
      `Cannot promote '${target}': it isn't a served entity on '${endpoint}'. Available: ${JSON.stringify(available)}`,
    );
  }

  const sortedNames = [...servedNames].sort();
  const routes: Route[] = sortedNames.map((name) => ({
    servedEntityName: name,
    trafficPercentage: name === target ? 100 : 0,
  }));

  await servingEndpointsClient.updateConfig(endpoint, {
    trafficConfig: { routes },
  });

  return getCanaryConfig({ endpoint, servingEndpointsClient });
}

/**
 * Send 100% of traffic to `version` — typically the prior production version.
 *
 * Functionally identical to `promoteCanary`; named separately so the call
 * site reads as the rollback semantic the operator intends.
 */
export async function rollbackCanary(opts: {
  endpoint: string;
  registeredModelName: string;
  version: number | string;
  servingEndpointsClient: ServingEndpointsClient;
}): Promise<CanaryConfig> {
  return promoteCanary(opts);
}

// ---------------------------------------------------------------------------
// Per-version analysis
// ---------------------------------------------------------------------------

const AnalyzeCanaryOpts = z.object({
  endpoint: z.string().min(1),
  experiment: z.string().min(1),
  lookbackHours: z.number().int().default(24),
  maxTraces: z.number().int().default(2000),
  traceSearch: z.custom<TraceSearch>((v) => typeof v === 'function'),
});

/**
 * Partition trace rows by served-entity version + report metrics per version.
 *
 * Pulls traces via the injected `traceSearch` callable, reads each trace's
 * served-entity attribute (`apx.model.endpoint_version` first, or version
 * parsed off the served-entity name), and aggregates requests / errors /
 * latency P50 / P95 / avg per version.
 */
export async function analyzeCanary(opts: {
  endpoint: string;
  experiment: string;
  lookbackHours?: number;
  maxTraces?: number;
  traceSearch: TraceSearch;
}): Promise<CanaryReport> {
  const { endpoint, experiment, lookbackHours, maxTraces, traceSearch } =
    AnalyzeCanaryOpts.parse(opts);

  let rows: TraceRow[];
  try {
    rows = await traceSearch({ experiment, maxTraces });
  } catch {
    return new CanaryReport({ endpoint, lookbackHours, versions: [] });
  }
  if (!Array.isArray(rows)) rows = [];

  // Aggregator: version -> {requests, errors, latencies}
  const buckets = new Map<
    string,
    { requests: number; errors: number; latencies: number[] }
  >();

  for (const r of rows) {
    const attrs = _traceAttrs(r);
    const ep = attrs['apx.model.endpoint'];
    if (ep !== undefined && ep !== null && ep !== endpoint) continue;

    const version = _traceVersion(attrs);
    let bucket = buckets.get(version);
    if (!bucket) {
      bucket = { requests: 0, errors: 0, latencies: [] };
      buckets.set(version, bucket);
    }
    bucket.requests += 1;
    const status = _traceStatus(r);
    if (status && !['OK', 'SUCCESS'].includes(String(status).toUpperCase())) {
      bucket.errors += 1;
    }
    const latency = _traceLatencyMs(r);
    if (latency !== null) {
      bucket.latencies.push(latency);
    }
  }

  const versionEntries = [...buckets.entries()].sort(([a], [b]) =>
    a < b ? -1 : a > b ? 1 : 0,
  );

  const versions: VersionMetrics[] = versionEntries.map(([version, bucket]) => {
    const latencies = [...bucket.latencies].sort((a, b) => a - b);
    return new VersionMetrics({
      version,
      requests: bucket.requests,
      errors: bucket.errors,
      latencyP50Ms: _percentile(latencies, 50),
      latencyP95Ms: _percentile(latencies, 95),
      latencyAvgMs:
        latencies.length > 0
          ? latencies.reduce((a, b) => a + b, 0) / latencies.length
          : null,
    });
  });

  return new CanaryReport({ endpoint, lookbackHours, versions });
}

// ---------------------------------------------------------------------------
// Trace helpers (duck-typed extraction)
// ---------------------------------------------------------------------------

function _traceAttrs(t: TraceRow): Record<string, unknown> {
  if (t == null) return {};
  if (typeof t === 'object' && !Array.isArray(t)) {
    if ('tags' in t || 'attributes' in t) {
      return (t.tags ?? t.attributes ?? {}) as Record<string, unknown>;
    }
    const info = t.info;
    if (info) {
      return (info.tags ?? {}) as Record<string, unknown>;
    }
  }
  return {};
}

function _traceVersion(attrs: Record<string, unknown>): string {
  const explicit = attrs['apx.model.endpoint_version'];
  if (explicit) return String(explicit);
  const servedEntity =
    attrs['mlflow.databricks.served_entity_name'] ?? attrs['apx.model.served_entity'];
  if (servedEntity && String(servedEntity).includes('-')) {
    const s = String(servedEntity);
    return s.slice(s.lastIndexOf('-') + 1);
  }
  return 'unknown';
}

function _traceStatus(t: TraceRow): string | null {
  if (t == null) return null;
  if ('status' in t && t.status != null) return String(t.status);
  const info = t.info;
  if (info?.status != null) return String(info.status);
  return null;
}

function _traceLatencyMs(t: TraceRow): number | null {
  let val: unknown;
  if (t == null) return null;
  if ('execution_time_ms' in t) {
    val = t.execution_time_ms;
  } else {
    val = t.info?.execution_time_ms;
  }
  if (val === null || val === undefined) return null;
  const n = Number(val);
  if (!Number.isFinite(n)) return null;
  return Math.trunc(n);
}

/** Nearest-rank percentile from a SORTED list (Python-compatible math). */
function _percentile(values: number[], percentile: number): number | null {
  if (values.length === 0) return null;
  if (percentile <= 0) return values[0];
  if (percentile >= 100) return values[values.length - 1];
  // Python: idx = max(0, min(len-1, int(round(len * pct / 100)) - 1))
  const raw = pythonRound((values.length * percentile) / 100) - 1;
  const idx = Math.max(0, Math.min(values.length - 1, raw));
  return values[idx];
}

/**
 * Python's `round()` uses banker's rounding (half-to-even); approximate it
 * sufficiently for the integer-percentage math here. For the values used
 * (proportional shares * remaining percentage), the differences vs Math.round
 * are immaterial — but matching Python's behavior on exact 0.5 cases keeps
 * the test math aligned.
 */
function pythonRound(x: number): number {
  // Banker's rounding (round half to even)
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff < 0.5) return floor;
  if (diff > 0.5) return floor + 1;
  // diff === 0.5 — pick even
  return floor % 2 === 0 ? floor : floor + 1;
}
