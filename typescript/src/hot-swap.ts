/**
 * hot-swap — change a deployed agent's LLM endpoint without re-logging.
 *
 * The runtime side reads `APX_AGENT_MODEL_OVERRIDE` and uses it instead
 * of the compile-time model. This module is the control side: writes the
 * env var onto a deployed serving endpoint so the next replica picks up
 * the new model.
 *
 * Use cases:
 *   * Swap from `claude-sonnet` to `claude-opus` to test a more
 *     capable model without re-deploying the agent artifact.
 *   * Roll back a problematic model change in seconds (set the override
 *     to the previous value returned by the prior call).
 *
 * For traffic-split A/B testing across two complete artifacts (not just
 * the LLM endpoint), see `canary.ts` instead.
 *
 * Mechanism: rewrites each served entity's `environmentVars` to include
 * the override. Other env vars are preserved. The update is durable
 * until the next `hotSwapModel` call or until the endpoint is redeployed.
 *
 * Caveats:
 *   * The new env var takes effect on the next replica start. With
 *     scale-to-zero, the next request triggers a cold start that picks
 *     it up. Without scale-to-zero, existing replicas may serve the
 *     old model until they cycle.
 */

import { z } from 'zod';
import type {
  ServedEntityInput,
  ServingEndpointsClient,
} from './canary.js';

// ---------------------------------------------------------------------------
// Constants + types
// ---------------------------------------------------------------------------

/**
 * The env var name the framework runtime reads to override the
 * compile-time model. Exposed as a module constant so callers can match
 * it in their own tooling.
 */
export const APX_MODEL_OVERRIDE_ENV = 'APX_AGENT_MODEL_OVERRIDE';

/** Outcome of a hot-swap operation. */
export interface HotSwapResult {
  readonly endpoint: string;
  readonly newModel: string;
  /** The value the override held before; null if first set. */
  readonly previousModel: string | null;
  /** Names of the served entities that were updated. */
  readonly servedEntities: string[];
}

// ---------------------------------------------------------------------------
// hotSwapModel
// ---------------------------------------------------------------------------

const HotSwapModelOpts = z.object({
  endpoint: z.string().min(1),
  model: z.string().min(1),
  servingEndpointsClient: z.custom<ServingEndpointsClient>(
    (v) => typeof v === 'object' && v !== null && 'get' in v && 'updateConfig' in v,
    'servingEndpointsClient must implement get/updateConfig',
  ),
  envVar: z.string().min(1).default(APX_MODEL_OVERRIDE_ENV),
  wait: z.boolean().default(true),
});

/**
 * Hot-swap a deployed agent's LLM endpoint by updating env vars.
 *
 * @returns `HotSwapResult` with the previous override value (if any) so
 *   the caller can roll back without re-discovering it.
 *
 * @throws when the endpoint has no served entities to update.
 */
export async function hotSwapModel(opts: {
  endpoint: string;
  model: string;
  servingEndpointsClient: ServingEndpointsClient;
  envVar?: string;
  wait?: boolean;
}): Promise<HotSwapResult> {
  const { endpoint, model, servingEndpointsClient, envVar } =
    HotSwapModelOpts.parse(opts);

  const info = await servingEndpointsClient.get(endpoint);
  const config = info?.config ?? info?.pendingConfig ?? null;
  const servedEntities = config?.servedEntities ?? config?.servedModels ?? null;

  if (!servedEntities || servedEntities.length === 0) {
    throw new Error(
      `Endpoint '${endpoint}' has no served entities to update. ` +
        'Has the agent been deployed yet?',
    );
  }

  let previousValue: string | null = null;
  const newServedEntities: ServedEntityInput[] = [];
  const updatedNames: string[] = [];

  for (const entity of servedEntities) {
    // Read existing env vars off the response — duck-typed since
    // ServedEntityResponse does not declare `environmentVars`.
    const existing = _readEnvironmentVars(entity);
    const merged: Record<string, string> = { ...existing };

    if (previousValue === null && envVar in merged) {
      previousValue = merged[envVar];
    }
    merged[envVar] = model;

    // Read other fields (also duck-typed; canary's ServedEntityResponse
    // is a subset, but the underlying object likely has more).
    const raw = entity as unknown as Record<string, unknown>;
    const name = _asString(raw['name']);
    const entityName = _asString(raw['entityName'] ?? raw['modelName']);
    const entityVersion = _asString(raw['entityVersion'] ?? raw['modelVersion']);
    const scaleToZero = _asBool(raw['scaleToZeroEnabled']);
    const workloadSize = _asString(raw['workloadSize']);

    newServedEntities.push({
      name: name ?? '',
      entityName: entityName ?? null,
      entityVersion: entityVersion ?? null,
      scaleToZeroEnabled: scaleToZero ?? true,
      workloadSize: workloadSize ?? 'Small',
      environmentVars: merged,
    });
    updatedNames.push(name ?? '');
  }

  await servingEndpointsClient.updateConfig(endpoint, {
    servedEntities: newServedEntities,
  });

  return Object.freeze({
    endpoint,
    newModel: model,
    previousModel: previousValue,
    servedEntities: updatedNames,
  });
}

// ---------------------------------------------------------------------------
// getActiveOverride
// ---------------------------------------------------------------------------

const GetActiveOverrideOpts = z.object({
  endpoint: z.string().min(1),
  servingEndpointsClient: z.custom<ServingEndpointsClient>(
    (v) => typeof v === 'object' && v !== null && 'get' in v && 'updateConfig' in v,
    'servingEndpointsClient must implement get/updateConfig',
  ),
  envVar: z.string().min(1).default(APX_MODEL_OVERRIDE_ENV),
});

/**
 * Return the current value of the model override env var, or null if
 * unset.
 *
 * Useful for verifying a swap landed and for showing operators which
 * model is actually serving (vs. the compile-time default).
 *
 * Mirrors the Python behavior: returns the value from the FIRST served
 * entity that has the env var set, or null if none do (or there are no
 * served entities at all).
 */
export async function getActiveOverride(opts: {
  endpoint: string;
  servingEndpointsClient: ServingEndpointsClient;
  envVar?: string;
}): Promise<string | null> {
  const { endpoint, servingEndpointsClient, envVar } =
    GetActiveOverrideOpts.parse(opts);

  const info = await servingEndpointsClient.get(endpoint);
  const config = info?.config ?? info?.pendingConfig ?? null;
  const servedEntities = config?.servedEntities ?? config?.servedModels ?? null;
  if (!servedEntities || servedEntities.length === 0) {
    return null;
  }

  for (const entity of servedEntities) {
    const env = _readEnvironmentVars(entity);
    if (envVar in env) {
      return env[envVar];
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Internal helpers — duck-typed reads off the SDK response.
// ---------------------------------------------------------------------------

function _readEnvironmentVars(entity: unknown): Record<string, string> {
  if (entity == null || typeof entity !== 'object') return {};
  const raw = entity as Record<string, unknown>;
  const env = raw['environmentVars'] ?? raw['environment_vars'];
  if (env && typeof env === 'object' && !Array.isArray(env)) {
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(env as Record<string, unknown>)) {
      out[k] = String(v);
    }
    return out;
  }
  return {};
}

function _asString(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  return String(v);
}

function _asBool(v: unknown): boolean | null {
  if (v === null || v === undefined) return null;
  return Boolean(v);
}
