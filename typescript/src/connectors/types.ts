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

/**
 * Resolve a Databricks bearer token for an outbound API call.
 *
 * Priority order — checked at call time so per-request OBO tokens are
 * always used when available, not captured at construction time:
 *   1. Explicit `oboHeaders` argument (e.g. passed from the incoming request)
 *   2. `AsyncLocalStorage` request context — set by the agent framework for
 *      every tool handler and sub-agent call; reads `x-forwarded-access-token`
 *      or `authorization` from the user's OBO headers
 *   3. `DATABRICKS_TOKEN` env var — local dev or service-to-service fallback
 *
 * @throws if no token is available through any path
 */
export function resolveToken(oboHeaders?: Record<string, string>): string {
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
  throw new Error(
    'No Databricks token available. Pass oboHeaders, run within a request context, or set DATABRICKS_TOKEN.',
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
