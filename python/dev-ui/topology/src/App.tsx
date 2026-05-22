// STUB — to be filled in by Agent 4 (Phase 1).
//
// Owner contract (see docs/superpowers/specs/2026-05-22-topology-ui.md):
//
//   - Owns selection state: `{ selected, data, loading, error }`.
//   - On mount, fetch `/_apx/topology.json` and populate `data`.
//   - During development (`import.meta.env.DEV`), fall back to sample-topology.json
//     when the fetch fails so the component can be developed without a running
//     backend.
//   - Render layout: TopBar | TopologyGraph (flex 1) | NodeInspector (when selected).
//   - TopBar shows agentName, a link back to /_apx/agent ("Open in chat"), and a
//     link to /_apx/edit ("Edit source"). Both wired to live in the same window.

import { useEffect, useState } from "react";
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

export default function App() {
  const [state, setState] = useState<AppState>({
    data: null,
    loading: true,
    error: null,
    selected: null,
  });

  useEffect(() => {
    fetch("/_apx/topology.json")
      .then((r) => (r.ok ? r.json() : Promise.reject(`HTTP ${r.status}`)))
      .then((data: TopologyResponse) => setState((s) => ({ ...s, data, loading: false })))
      .catch((err: unknown) => {
        const message = err instanceof Error ? err.message : String(err);
        if (import.meta.env.DEV) {
          setState({
            data: sampleData as TopologyResponse,
            loading: false,
            error: null,
            selected: null,
          });
          return;
        }
        setState((s) => ({ ...s, error: message, loading: false }));
      });
  }, []);

  if (state.loading) {
    return <div style={{ padding: 20 }}>Loading topology…</div>;
  }
  if (state.error || !state.data) {
    return <div style={{ padding: 20, color: "#f87171" }}>Error: {state.error ?? "no data"}</div>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "10px 16px",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <div>
          <strong>{state.data.agentName}</strong>
          <span style={{ color: "var(--text-muted)", marginLeft: 8 }}>topology</span>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <a href="/_apx/agent">Open in chat</a>
          <a href="/_apx/edit">Edit source</a>
        </div>
      </header>
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        <div style={{ flex: 1, position: "relative" }}>
          <TopologyGraph
            data={state.data}
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
    </div>
  );
}
