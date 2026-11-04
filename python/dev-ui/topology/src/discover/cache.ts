// Tab-local only. Identity must be fetched afresh before constructing these keys.
import type { AgentContext, GraphResponse } from "./types";

export const CACHE_TTL = 5 * 60 * 1000;
const PREFIX = "apx-discover:v1:";

export function normalizeScope(scope: string): string {
  return [...new Set(scope.split(",").map((c) => c.trim()).filter(Boolean))].sort().join(",");
}

export function identityKey(context: AgentContext): string | null {
  if (!context.host || !context.user_id || context.user_id === "unknown") {
    throw new Error("Could not verify workspace identity. Try again to load Discover safely.");
  }
  // Local servers can host different projects at the same origin. Without a
  // stable app/project namespace, browse live but never persist or reuse metadata.
  if (!context.app_url && !context.project_id) return null;
  return PREFIX + JSON.stringify([window.location.origin, window.location.pathname,
    context.host, context.app_url, context.project_id, context.user_id]);
}

// Storage is optional (disabled browser storage, quota, or corrupt entries => cache miss).
export function readStored<T>(key: string): T | null {
  try {
    const value = sessionStorage.getItem(key);
    return value === null ? null : JSON.parse(value) as T;
  } catch {
    return null;
  }
}

export function writeStored(key: string, value: unknown): void {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Browsing still works without the optional reload cache.
  }
}

export interface CachedGraph { updated: number; graph: GraphResponse }
export function graphKey(identity: string, scope: string): string {
  return `${identity}:graph:${normalizeScope(scope)}`;
}

export function readGraph(key: string): CachedGraph | null {
  const entry = readStored<CachedGraph>(key);
  if (!entry || !Number.isFinite(entry.updated) || entry.updated > Date.now()
      || Date.now() - entry.updated >= CACHE_TTL || !Array.isArray(entry.graph?.nodes)
      || !Array.isArray(entry.graph.edges) || !Array.isArray(entry.graph.warnings) || !entry.graph.agent) return null;
  return entry;
}

export function forgetGraph(key: string): void {
  try {
    sessionStorage.removeItem(key);
  } catch {
    // No readable storage means no reusable cache entry either.
  }
}

export interface DiscoverPreferences {
  catalogs: string;
  queryInput: string;
  hiddenTypes: string[];
  onlyAgent: boolean;
}

export function readPreferences(identity: string): DiscoverPreferences | null {
  const prefs = readStored<DiscoverPreferences>(`${identity}:preferences`);
  return prefs && typeof prefs.catalogs === "string" && typeof prefs.queryInput === "string"
    && Array.isArray(prefs.hiddenTypes) && prefs.hiddenTypes.every((t) => typeof t === "string")
    && typeof prefs.onlyAgent === "boolean" ? prefs : null;
}
