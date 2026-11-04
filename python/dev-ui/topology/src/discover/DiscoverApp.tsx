// /_apx/discover — the caller's workspace graph (same data the agent tools see).
// Left rail: search, type filters, catalog scope, Refresh. Canvas + detail panel.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { NAV_LINKS } from "../nav";
import { AgentPanel } from "./AgentPanel";
import { DetailPanel } from "./DetailPanel";
import { DiscoverGraph } from "./DiscoverGraph";
import { EDGE_LEGEND, NODE_COLORS, NODE_TYPES, type AgentContext, type GraphNode, type GraphResponse } from "./types";

import { forgetGraph, graphKey, identityKey, normalizeScope, readGraph, readPreferences, writeStored } from "./cache";

export default function DiscoverApp() {
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [catalogs, setCatalogs] = useState("");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [hiddenTypes, setHiddenTypes] = useState<ReadonlySet<string>>(new Set());
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [impacted, setImpacted] = useState<ReadonlySet<string> | null>(null);
  const [impactError, setImpactError] = useState<string | null>(null);
  const [loadedScope, setLoadedScope] = useState("");
  const [context, setContext] = useState<AgentContext | null>(null);
  const [onlyAgent, setOnlyAgent] = useState(false);
  const [identity, setIdentity] = useState<string | null>(null);
  const [cacheStatus, setCacheStatus] = useState<{ updated: number; cached: boolean } | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const hydratedIdentity = useRef<string | null>(null);

  useEffect(() => {
    if (identity && !loading) writeStored(`${identity}:preferences`, {
      catalogs, queryInput, hiddenTypes: [...hiddenTypes], onlyAgent,
    });
  }, [identity, loading, catalogs, queryInput, hiddenTypes, onlyAgent]);

  // Debounce: the input stays responsive, but dagre only re-runs once typing pauses.
  useEffect(() => {
    const t = setTimeout(() => setQuery(queryInput), 250);
    return () => clearTimeout(t);
  }, [queryInput]);

  const load = useCallback(async (requestedScope: string | null, refresh: boolean) => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    const { signal } = controller;
    setLoading(true);
    setError(null);
    // Remove every previous identity's metadata before checking the current caller.
    setGraph(null);
    setContext(null);
    setIdentity(null);
    setSelected(null);
    setImpacted(null);
    setImpactError(null);
    setCacheStatus(null);
    try {
      const response = await fetch("/_apx/workspace-context", { cache: "no-store", signal });
      if (!response.ok) throw new Error(`Could not verify workspace identity (HTTP ${response.status}).`);
      const d = await response.json() as AgentContext;
      if (signal.aborted) return;
      const key = identityKey(d); // No unknown/display-name identity may authorize cache reuse.
      const prefs = key ? readPreferences(key) : null;
      const scope = normalizeScope(requestedScope ?? prefs?.catalogs ?? d.project_catalog ?? d.used_catalogs.join(","));
      setContext(d);
      setIdentity(key);
      setCatalogs(scope);
      setLoadedScope(scope);
      if (hydratedIdentity.current !== key) {
        setQueryInput(prefs?.queryInput ?? "");
        setHiddenTypes(new Set(prefs?.hiddenTypes ?? []));
        setOnlyAgent(prefs?.onlyAgent ?? false);
        hydratedIdentity.current = key;
      }
      const cacheKey = key ? graphKey(key, scope) : null;
      if (refresh && cacheKey) forgetGraph(cacheKey);
      const cached = !refresh && cacheKey ? readGraph(cacheKey) : null;
      let g: GraphResponse;
      let updated: number;
      if (cached) {
        g = cached.graph;
        updated = cached.updated;
      } else {
        const q = new URLSearchParams();
        if (scope) q.set("catalogs", scope);
        if (refresh) q.set("refresh", "1");
        const r = await fetch(`/_apx/discover/graph.json?${q}`, { cache: "no-store", signal });
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 300)}`);
        g = await r.json() as GraphResponse;
        updated = Date.now();
        if (signal.aborted) return;
        if (cacheKey) writeStored(cacheKey, { updated, graph: g });
      }
      if (signal.aborted) return;
      setGraph(g);
      setCacheStatus({ updated, cached: cached !== null });
      setCollapsed(new Set(g.nodes.filter((n) => n.type === "Schema").map((n) => n.id)));
    } catch (e: unknown) {
      if (!signal.aborted) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(null, false);
    return () => activeRequest.current?.abort();
  }, [load]);

  const byId = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const counts = useMemo(() => {
    const c = new Map<string, number>();
    for (const n of graph?.nodes ?? []) c.set(n.type, (c.get(n.type) ?? 0) + 1);
    return c;
  }, [graph]);

  const { nodes, edges, collapsedCounts } = useMemo(() => {
    const all = graph?.nodes ?? [];
    // "Only this agent's assets": its node and footprint, with schemas never collapsed over them.
    const agentIds = onlyAgent && graph ? new Set([graph.agent.id, ...graph.agent.footprint]) : null;
    const folded = agentIds ? new Set<string>() : collapsed;
    const q = query.trim().toLowerCase();
    const matches = (n: GraphNode) =>
      !q || n.name.toLowerCase().includes(q) || n.fqn.toLowerCase().includes(q);
    const byNodeId = new Map(all.map((n) => [n.id, n]));
    const hiddenBySchema = new Map<string, number>();
    const hiddenIds = new Set<string>();
    const revealedSchemas = new Set<string>();
    for (const e of graph?.edges ?? []) {
      if (e.type === "contains" && folded.has(e.src)) {
        const child = byNodeId.get(e.dst);
        // A search match inside a collapsed schema is shown, and keeps its schema visible.
        if (q && child && !hiddenTypes.has(child.type) && matches(child)) {
          revealedSchemas.add(e.src);
          continue;
        }
        hiddenIds.add(e.dst);
        hiddenBySchema.set(e.src, (hiddenBySchema.get(e.src) ?? 0) + 1);
      }
    }
    const visible = all.filter((n) =>
      !hiddenTypes.has(n.type) && !hiddenIds.has(n.id) && (!agentIds || agentIds.has(n.id))
      && (matches(n) || revealedSchemas.has(n.id)));
    const ids = new Set(visible.map((n) => n.id));
    return {
      nodes: visible,
      edges: (graph?.edges ?? []).filter((e) => ids.has(e.src) && ids.has(e.dst)),
      collapsedCounts: hiddenBySchema,
    };
  }, [graph, hiddenTypes, collapsed, query, onlyAgent]);

  const runImpact = (id: string) => {
    setImpactError(null);
    // Impact uses the scope the graph was loaded with, not whatever is typed in the box now.
    const q = loadedScope ? `?catalogs=${encodeURIComponent(loadedScope)}` : "";
    fetch(`/_apx/discover/impact/${encodeURIComponent(id)}${q}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { impacted: string[] }) => setImpacted(new Set(d.impacted)))
      .catch((e: unknown) => setImpactError(e instanceof Error ? e.message : String(e)));
  };

  const selectNode = (id: string | null) => {
    setSelected(id);
    setImpacted(null);
    setImpactError(null);
  };

  const toggle = (set: ReadonlySet<string>, v: string) => {
    const next = new Set(set);
    if (next.has(v)) next.delete(v); else next.add(v);
    return next;
  };

  const sel = selected ? byId.get(selected) : undefined;
  const isEmbedded = typeof window !== "undefined" && window.self !== window.top;

  return (
    <div className="apx-app">
      {!isEmbedded && <div id="apx-header">
        <div id="apx-nav">
          <span className="badge">APX dev</span>
          <span className="apx-nav-meta">
            {graph ? `${graph.nodes.length} assets · ${graph.edges.length} edges` : "workspace graph"}
          </span>
          <nav>
            {NAV_LINKS.filter((l) => l.slug !== "agent").map((l) => (
              <a key={l.href} href={l.href} className={l.slug === "discover" ? "active" : undefined}>{l.label}</a>
            ))}
          </nav>
        </div>
      </div>}
      <main className={`apx-main ${isEmbedded ? "" : "with-nav"}`}>
        <div className="apx-content">
          <aside className="apx-palette">
            <div className="apx-palette-head">
              <div className="apx-palette-title">Workspace graph</div>
              <div className="apx-palette-sub">Assets visible to your identity — the same graph your agent's tools query.</div>
            </div>
            <AgentPanel overlay={graph?.agent ?? null} context={context} onlyAgent={onlyAgent}
              onToggleOnly={() => setOnlyAgent((v) => !v)}
              onSelectAgent={() => graph && selectNode(graph.agent.id)} />
            <input className="apx-dg-input" placeholder="Search name or fqn" aria-label="Search assets"
              value={queryInput} onChange={(e) => setQueryInput(e.target.value)} />
            <label className="apx-palette-sub" htmlFor="apx-dg-catalogs">Catalogs (comma-separated; empty = first 10)</label>
            <input id="apx-dg-catalogs" className="apx-dg-input" value={catalogs} onChange={(e) => setCatalogs(e.target.value)}
              list="apx-dg-catalog-list" onKeyDown={(e) => { if (e.key === "Enter") load(catalogs, false); }} />
            <datalist id="apx-dg-catalog-list">
              {(graph?.nodes ?? []).filter((n) => n.type === "Catalog").map((n) => <option key={n.id} value={n.name} />)}
            </datalist>
            <div className="apx-palette-actions">
              <button type="button" className="apx-btn" disabled={loading} onClick={() => load(catalogs, false)}>Load</button>
              <button type="button" className="apx-btn" disabled={loading} onClick={() => load(catalogs, true)}>Refresh</button>
            </div>
            <div className="apx-palette-list">
              {NODE_TYPES.map((t) => (
                <label key={t} className="apx-dg-filter">
                  <input type="checkbox" checked={!hiddenTypes.has(t)} onChange={() => setHiddenTypes((s) => toggle(s, t))} />
                  <span className="apx-dg-swatch" style={{ background: NODE_COLORS[t] }} />
                  {t} <span className="apx-dg-muted">{counts.get(t) ?? 0}</span>
                </label>
              ))}
            </div>
            <details className="apx-dg-legend">
              <summary>Edge types</summary>
              {EDGE_LEGEND.map((e) => (
                <div key={e.type} className="apx-dg-legend-row">
                  <span className="apx-dg-line" style={{ borderTop: `2px ${e.dashed ? "dashed" : "solid"} ${e.color}` }} />
                  <code>{e.type}</code> <span className="apx-dg-muted">{e.description}</span>
                </div>
              ))}
            </details>
            {cacheStatus && <div className="apx-dg-muted" role="status">
              {cacheStatus.cached ? "Cached" : "Loaded"} · updated {new Date(cacheStatus.updated).toLocaleTimeString()}
            </div>}
            {graph && <div className="apx-dg-muted">built {new Date(graph.built_at).toLocaleTimeString()}</div>}
          </aside>
          <div className="apx-workspace">
            {graph && graph.warnings.length > 0 && (
              <div className="apx-dg-warnings" role="status">
                Partial graph — {graph.warnings.length} source{graph.warnings.length === 1 ? "" : "s"} could not be read:
                <ul>{graph.warnings.map((w, i) => <li key={`${i}-${w}`}>{w}</li>)}</ul>
              </div>
            )}
            <div className="apx-graph">
              {loading ? (
                <div className="apx-loading"><div className="apx-spinner" aria-hidden="true" /><div>Building workspace graph&hellip;</div></div>
              ) : error ? (
                <div className="apx-empty">
                  <div className="apx-empty-title">Could not load the workspace graph</div>
                  <div className="apx-empty-body">{error}</div>
                  <button type="button" className="apx-btn" onClick={() => load(null, false)}>Try again</button>
                </div>
              ) : nodes.length === 0 ? (
                <div className="apx-empty"><div className="apx-empty-title">No assets match</div>
                  <div className="apx-empty-body">Change the catalog scope, filters, or search.</div></div>
              ) : (
                <DiscoverGraph nodes={nodes} edges={edges} selected={selected} impacted={impacted}
                  collapsedCounts={collapsedCounts} onSelect={selectNode}
                  onToggleSchema={(id) => setCollapsed((s) => toggle(s, id))} />
              )}
            </div>
          </div>
          {sel && graph && (
            <DetailPanel node={sel} edges={graph.edges} byId={byId}
              impactCount={impacted ? impacted.size : null} impactError={impactError}
              onSelect={selectNode} onImpact={() => runImpact(sel.id)}
              onClearImpact={() => setImpacted(null)} onClose={() => selectNode(null)} />
          )}
        </div>
      </main>
    </div>
  );
}
