// /_apx/discover — the caller's workspace graph (same data the agent tools see).
// Left rail: search, type filters, catalog scope, Refresh. Canvas + detail panel.
import { useCallback, useEffect, useMemo, useState } from "react";

import { NAV_LINKS } from "../nav";
import { DetailPanel } from "./DetailPanel";
import { DiscoverGraph } from "./DiscoverGraph";
import { EDGE_LEGEND, NODE_COLORS, NODE_TYPES, type GraphResponse } from "./types";

const COLLAPSE_ABOVE = 2000; // Lattice: collapse schemas instead of virtualizing

export default function DiscoverApp() {
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [catalogs, setCatalogs] = useState("");
  const [query, setQuery] = useState("");
  const [hiddenTypes, setHiddenTypes] = useState<ReadonlySet<string>>(new Set());
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [impacted, setImpacted] = useState<ReadonlySet<string> | null>(null);

  const load = useCallback((scope: string, refresh: boolean) => {
    setLoading(true);
    setError(null);
    const q = new URLSearchParams();
    if (scope.trim()) q.set("catalogs", scope.trim());
    if (refresh) q.set("refresh", "1");
    fetch(`/_apx/discover/graph.json?${q}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 300)}`);
        return (await r.json()) as GraphResponse;
      })
      .then((g) => {
        setGraph(g);
        setImpacted(null);
        setCollapsed(new Set(g.nodes.length > COLLAPSE_ABOVE ? g.nodes.filter((n) => n.type === "Schema").map((n) => n.id) : []));
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetch("/_apx/workspace-context")
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null)
      .then((d: { used_catalogs?: string[] } | null) => {
        const scope = (d?.used_catalogs ?? []).join(",");
        setCatalogs(scope);
        load(scope, false);
      });
  }, [load]);

  const byId = useMemo(() => new Map((graph?.nodes ?? []).map((n) => [n.id, n])), [graph]);
  const counts = useMemo(() => {
    const c = new Map<string, number>();
    for (const n of graph?.nodes ?? []) c.set(n.type, (c.get(n.type) ?? 0) + 1);
    return c;
  }, [graph]);

  const { nodes, edges, collapsedCounts } = useMemo(() => {
    const all = graph?.nodes ?? [];
    const hiddenBySchema = new Map<string, number>();
    const hiddenIds = new Set<string>();
    for (const e of graph?.edges ?? []) {
      if (e.type === "contains" && collapsed.has(e.src)) {
        hiddenIds.add(e.dst);
        hiddenBySchema.set(e.src, (hiddenBySchema.get(e.src) ?? 0) + 1);
      }
    }
    const q = query.trim().toLowerCase();
    const visible = all.filter((n) =>
      !hiddenTypes.has(n.type) && !hiddenIds.has(n.id)
      && (!q || n.name.toLowerCase().includes(q) || n.fqn.toLowerCase().includes(q)));
    const ids = new Set(visible.map((n) => n.id));
    return {
      nodes: visible,
      edges: (graph?.edges ?? []).filter((e) => ids.has(e.src) && ids.has(e.dst)),
      collapsedCounts: hiddenBySchema,
    };
  }, [graph, hiddenTypes, collapsed, query]);

  const runImpact = (id: string) => {
    const q = catalogs.trim() ? `?catalogs=${encodeURIComponent(catalogs.trim())}` : "";
    fetch(`/_apx/discover/impact/${encodeURIComponent(id)}${q}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d: { impacted: string[] }) => setImpacted(new Set(d.impacted)))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  };

  const toggle = (set: ReadonlySet<string>, v: string) => {
    const next = new Set(set);
    if (next.has(v)) next.delete(v); else next.add(v);
    return next;
  };

  const sel = selected ? byId.get(selected) : undefined;

  return (
    <div className="apx-app">
      <div id="apx-header">
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
      </div>
      <main className="apx-main with-nav">
        <div className="apx-content">
          <aside className="apx-palette">
            <div className="apx-palette-head">
              <div className="apx-palette-title">Workspace graph</div>
              <div className="apx-palette-sub">Assets visible to your identity — the same graph your agent's tools query.</div>
            </div>
            <input className="apx-dg-input" placeholder="Search name or fqn" value={query} onChange={(e) => setQuery(e.target.value)} />
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
            {graph && <div className="apx-dg-muted">built {new Date(graph.built_at).toLocaleTimeString()}</div>}
          </aside>
          <div className="apx-workspace">
            {graph && graph.warnings.length > 0 && (
              <div className="apx-dg-warnings" role="status">
                Partial graph — {graph.warnings.length} source{graph.warnings.length === 1 ? "" : "s"} could not be read:
                <ul>{graph.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
              </div>
            )}
            <div className="apx-graph">
              {loading ? (
                <div className="apx-loading"><div className="apx-spinner" aria-hidden="true" /><div>Building workspace graph&hellip;</div></div>
              ) : error ? (
                <div className="apx-empty">
                  <div className="apx-empty-title">Could not load the workspace graph</div>
                  <div className="apx-empty-body">{error}</div>
                  <button type="button" className="apx-btn" onClick={() => load(catalogs, false)}>Try again</button>
                </div>
              ) : nodes.length === 0 ? (
                <div className="apx-empty"><div className="apx-empty-title">No assets match</div>
                  <div className="apx-empty-body">Change the catalog scope, filters, or search.</div></div>
              ) : (
                <DiscoverGraph nodes={nodes} edges={edges} selected={selected} impacted={impacted}
                  collapsedCounts={collapsedCounts} onSelect={setSelected}
                  onToggleSchema={(id) => setCollapsed((s) => toggle(s, id))} />
              )}
            </div>
          </div>
          {sel && graph && (
            <DetailPanel node={sel} edges={graph.edges} byId={byId}
              impactCount={impacted ? impacted.size : null}
              onSelect={setSelected} onImpact={() => runImpact(sel.id)}
              onClearImpact={() => setImpacted(null)} onClose={() => { setSelected(null); setImpacted(null); }} />
          )}
        </div>
      </main>
    </div>
  );
}
