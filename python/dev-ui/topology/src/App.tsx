// App shell for /_apx/topology — owns selection state, fetches the topology JSON,
// and wires together <TopologyGraph /> and <NodeInspector />.
//
// Contract (see docs/superpowers/specs/2026-05-22-topology-ui.md):
//   - Owns selection state: `{ data, loading, error, selected }`.
//   - On mount (and on Refresh), fetch `/_apx/topology.json`.
//   - In dev (`import.meta.env.DEV`), fall back to sample-topology.json on fetch
//     failure so the UI is workable without a running backend.
//   - Render: TopBar | TopologyGraph (flex 1) | NodeInspector (when selected).

import { useCallback, useEffect, useState } from "react";
import { TopologyGraph } from "./TopologyGraph";
import { NodeInspector } from "./NodeInspector";
import type { TopologyResponse } from "./types";
import sampleData from "./sample-topology.json";

interface AppState {
  data: TopologyResponse | null;
  loading: boolean;
  error: string | null;
  selected: string | null;
}

const NAV_LINKS: ReadonlyArray<{ href: string; label: string; icon: string }> = [
  { href: "/_apx/agent", label: "Open in chat", icon: "\u{1F4AC}" }, // speech bubble
  { href: "/_apx/edit", label: "Edit source", icon: "\u{270F}\u{FE0F}" }, // pencil
  { href: "/_apx/eval", label: "Eval", icon: "\u{2705}" }, // check mark
  { href: "/_apx/setup", label: "Setup", icon: "\u{2699}\u{FE0F}" }, // gear
];

export default function App() {
  const [state, setState] = useState<AppState>({
    data: null,
    loading: true,
    error: null,
    selected: null,
  });

  const load = useCallback(() => {
    setState((s) => ({ ...s, loading: true, error: null }));
    fetch("/_apx/topology.json")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: TopologyResponse) =>
        setState((s) => ({ ...s, data, loading: false, error: null })),
      )
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        if (import.meta.env.DEV) {
          setState((s) => ({
            ...s,
            data: sampleData as TopologyResponse,
            loading: false,
            error: null,
          }));
          return;
        }
        setState((s) => ({ ...s, error: message, loading: false }));
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const data = state.data;
  const nodeCount = data?.nodes.length ?? 0;
  const edgeCount = data?.edges.length ?? 0;
  const agentName = data?.agentName ?? "apx-agent";

  return (
    <div className="apx-app">
      <header className="apx-header">
        <div className="apx-title">
          <div className="apx-agent-name">{agentName}</div>
          <div className="apx-counter">
            topology &middot; {nodeCount} {nodeCount === 1 ? "node" : "nodes"} &middot;{" "}
            {edgeCount} {edgeCount === 1 ? "edge" : "edges"}
          </div>
        </div>
        <nav className="apx-nav">
          <button
            type="button"
            className="apx-nav-link apx-refresh"
            onClick={load}
            disabled={state.loading}
            title="Re-fetch /_apx/topology.json"
          >
            <span className="apx-nav-icon" aria-hidden="true">
              {"\u{21BB}"}
            </span>
            <span>Refresh</span>
          </button>
          {NAV_LINKS.map((link) => (
            <a key={link.href} href={link.href} className="apx-nav-link">
              <span className="apx-nav-icon" aria-hidden="true">
                {link.icon}
              </span>
              <span>{link.label}</span>
            </a>
          ))}
        </nav>
      </header>

      <main className="apx-main">
        {state.loading ? (
          <div className="apx-loading">
            <div className="apx-spinner" aria-hidden="true" />
            <div>Loading topology&hellip;</div>
          </div>
        ) : state.error || !data ? (
          <div className="apx-empty">
            <div className="apx-empty-title">Could not load topology</div>
            <div className="apx-empty-body">{state.error ?? "No data returned."}</div>
            <button type="button" className="apx-empty-action" onClick={load}>
              Try again
            </button>
          </div>
        ) : data.nodes.length === 0 ? (
          <div className="apx-empty">
            <div className="apx-empty-title">No tools or sub-agents yet</div>
            <div className="apx-empty-body">
              This agent has no tools or sub-agents yet. Add some in{" "}
              <a href="/_apx/edit">/_apx/edit</a>, then refresh.
            </div>
            <button type="button" className="apx-empty-action" onClick={load}>
              Refresh
            </button>
          </div>
        ) : (
          <div className="apx-content">
            <div className="apx-graph">
              <TopologyGraph
                data={data}
                selected={state.selected}
                onNodeClick={(id) => setState((s) => ({ ...s, selected: id }))}
              />
            </div>
            {state.selected && (
              <NodeInspector
                nodeId={state.selected}
                onClose={() => setState((s) => ({ ...s, selected: null }))}
              />
            )}
          </div>
        )}
      </main>
    </div>
  );
}
