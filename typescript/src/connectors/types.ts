import { z } from 'zod';
import { getRequestContext } from '../agent/request-context.js';

export interface FieldDef {
  name: string;
  type: string;
  key?: boolean;
  nullable?: boolean;
  default?: number | string;
  index?: boolean;
}

export interface EntityDef {
  name: string;
  table: string;
  fields: FieldDef[];
  embedding_source?: string;
}

export interface EdgeDef {
  name: string;
  table: string;
  from: string;
  to: string;
  fields: FieldDef[];
}

export interface ExtractionConfig {
  prompt_template: string;
  chunk_size: number;
  chunk_overlap: number;
}

export interface FitnessConfig {
  metric: string;
  evaluation: string;
  targets: Record<string, number>;
}

export interface EvolutionConfig {
  population_size: number;
  mutation_rate: number;
  mutation_fields: string[];
  selection: string;
  max_generations: number;
}

export interface EntitySchema {
  version: number;
  generation: number;
  entities: EntityDef[];
  edges: EdgeDef[];
  extraction: ExtractionConfig;
  fitness: FitnessConfig;
  evolution: EvolutionConfig;
}

export interface ConnectorConfig {
  host?: string;
  catalog: string;
  schema: string;
  vectorSearchIndex?: string;
  volumePath?: string;
  entitySchema?: EntitySchema;
}

const fieldDefSchema = z.object({
  name: z.string(),
  type: z.string(),
  key: z.boolean().optional(),
  nullable: z.boolean().optional(),
  default: z.union([z.number(), z.string()]).optional(),
  index: z.boolean().optional(),
});

const entityDefSchema = z.object({
  name: z.string(),
  table: z.string(),
  fields: z.array(fieldDefSchema),
  embedding_source: z.string().optional(),
});

const edgeDefSchema = z.object({
  name: z.string(),
  table: z.string(),
  from: z.string(),
  to: z.string(),
  fields: z.array(fieldDefSchema),
});

const extractionSchema = z.object({
  prompt_template: z.string(),
  chunk_size: z.number().int().min(1),
  chunk_overlap: z.number().int().min(0),
});

const fitnessSchema = z.object({
  metric: z.string(),
  evaluation: z.string(),
  targets: z.record(z.string(), z.number()),
});

const evolutionSchema = z.object({
  population_size: z.number().int().min(1),
  mutation_rate: z.number().min(0).max(1),
  mutation_fields: z.array(z.string()),
  selection: z.string(),
  max_generations: z.number().int().min(1),
});

const entitySchemaValidator = z.object({
  version: z.number().int(),
  generation: z.number().int().min(0),
  entities: z.array(entityDefSchema),
  edges: z.array(edgeDefSchema),
  extraction: extractionSchema,
  fitness: fitnessSchema,
  evolution: evolutionSchema,
});

export function parseEntitySchema(raw: unknown): EntitySchema {
  return entitySchemaValidator.parse(raw);
}

// ---------------------------------------------------------------------------
// M2M OAuth client credentials token cache
// ---------------------------------------------------------------------------

let m2mToken: string | null = null;
let m2mExpiry = 0;
let m2mInFlight: Promise<string> | null = null;

/**
 * Exchange DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET for an OAuth
 * access token via the Databricks OIDC token endpoint. The token is cached
 * and refreshed 60 seconds before expiry.
 *
 * This is the standard OAuth 2.0 client_credentials grant — the same flow
 * that Databricks Jobs, Workflows, and service principals use.
 */
async function acquireM2mToken(): Promise<string> {
  const clientId = process.env.DATABRICKS_CLIENT_ID;
  const clientSecret = process.env.DATABRICKS_CLIENT_SECRET;
  if (!clientId || !clientSecret) {
    throw new Error(
      'No Databricks token available. Provide one of:\n' +
      '  - X-Forwarded-Access-Token header (interactive/OBO)\n' +
      '  - DATABRICKS_TOKEN env var (static PAT)\n' +
      '  - DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET (M2M OAuth)',
    );
  }

  const host = resolveHost();
  const tokenUrl = `${host}/oidc/v1/token`;

  const res = await fetch(tokenUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'client_credentials',
      client_id: clientId,
      client_secret: clientSecret,
      scope: 'all-apis',
    }),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`M2M token exchange failed (${res.status}): ${text}`);
  }

  const data = await res.json() as { access_token: string; expires_in?: number };
  m2mToken = data.access_token;
  // Refresh 60s before expiry; default to 1 hour if expires_in not provided
  m2mExpiry = Date.now() + ((data.expires_in ?? 3600) - 60) * 1000;
  return m2mToken;
}

/**
 * Get a cached M2M token, refreshing if expired. Deduplicates concurrent
 * requests so only one token exchange is in-flight at a time.
 */
async function getM2mToken(): Promise<string> {
  if (m2mToken && Date.now() < m2mExpiry) return m2mToken;
  if (!m2mInFlight) {
    m2mInFlight = acquireM2mToken().finally(() => { m2mInFlight = null; });
  }
  return m2mInFlight;
}

// ---------------------------------------------------------------------------
// resolveToken
// ---------------------------------------------------------------------------

/**
 * Resolve a Databricks bearer token for an outbound API call.
 *
 * Priority order — checked at call time so per-request OBO tokens are
 * always used when available, not captured at construction time:
 *   1. Explicit `oboHeaders` argument (e.g. passed from the incoming request)
 *   2. `AsyncLocalStorage` request context — set by the agent framework for
 *      every tool handler and sub-agent call; reads `x-forwarded-access-token`
 *      or `authorization` from the user's OBO headers
 *   3. `DATABRICKS_TOKEN` env var — static PAT for local dev
 *   4. M2M OAuth via `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` —
 *      service principal identity for jobs, workflows, and background loops
 *
 * Steps 1-3 are synchronous. Step 4 requires an async token exchange, so
 * this function returns `string | Promise<string>`. All call sites already
 * run in async handlers, so `await resolveToken()` works everywhere.
 */
export function resolveToken(oboHeaders?: Record<string, string>): string | Promise<string> {
  if (oboHeaders) {
    const auth = oboHeaders['authorization'] ?? oboHeaders['Authorization'];
    if (auth?.startsWith('Bearer ')) return auth.slice(7);
    const xfat = oboHeaders['x-forwarded-access-token'];
    if (xfat) return xfat;
  }
  const ctx = getRequestContext();
  if (ctx) {
    const token =
      ctx.oboHeaders['x-forwarded-access-token'] ||
      (ctx.oboHeaders['authorization'] ?? '').replace(/^Bearer\s+/i, '');
    if (token) return token;
  }
  const envToken = process.env.DATABRICKS_TOKEN;
  if (envToken) return envToken;
  // M2M OAuth — returns cached token synchronously if available, else async exchange
  if (process.env.DATABRICKS_CLIENT_ID && process.env.DATABRICKS_CLIENT_SECRET) {
    if (m2mToken && Date.now() < m2mExpiry) return m2mToken;
    return getM2mToken();
  }
  throw new Error(
    'No Databricks token available. Provide one of:\n' +
    '  - X-Forwarded-Access-Token header (interactive/OBO)\n' +
    '  - DATABRICKS_TOKEN env var (static PAT)\n' +
    '  - DATABRICKS_CLIENT_ID + DATABRICKS_CLIENT_SECRET (M2M OAuth)',
  );
}

export function resolveHost(host?: string): string {
  const h = host ?? process.env.DATABRICKS_HOST;
  if (!h) throw new Error('No Databricks host: pass host in config or set DATABRICKS_HOST env var');
  const normalized = h.startsWith('http') ? h : `https://${h}`;
  return normalized.replace(/\/$/, '');
}

export interface SqlParam {
  name: string;
  value: string;
  type: 'STRING' | 'INT' | 'FLOAT' | 'BOOLEAN';
}

export function buildSqlParams(filters: Record<string, unknown>): {
  clause: string;
  params: SqlParam[];
} {
  const entries = Object.entries(filters);
  if (entries.length === 0) return { clause: '', params: [] };

  const params: SqlParam[] = entries.map(([key, value]) => {
    let type: SqlParam['type'] = 'STRING';
    if (typeof value === 'number') {
      type = Number.isInteger(value) ? 'INT' : 'FLOAT';
    } else if (typeof value === 'boolean') {
      type = 'BOOLEAN';
    }
    return { name: key, value: String(value), type };
  });

  const clause = entries.map(([key]) => `${key} = :${key}`).join(' AND ');
  return { clause, params };
}

export interface DbFetchOptions {
  token: string;
  method: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  body?: unknown;
}

export async function dbFetch<T = unknown>(url: string, opts: DbFetchOptions): Promise<T> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${opts.token}`,
  };

  const init: RequestInit = {
    method: opts.method,
    headers,
  };

  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  }

  const res = await fetch(url, init);

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Databricks API ${res.status}: ${text}`);
  }

  return res.json() as Promise<T>;
}
