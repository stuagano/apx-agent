// STUB — to be filled in by Agent 3 (Phase 1).
//
// Owner contract (see docs/superpowers/specs/2026-05-22-topology-ui.md):
//
//   - Right-side details panel.
//   - On mount + when `nodeId` changes, fetch `/_apx/topology/inspect/{nodeId}`.
//   - Show a loading state while in flight, a friendly error if the fetch fails.
//   - Render one of: AgentDetails, ToolDetails, ResourceDetails, SubAgentDetails
//     (the InspectResponse always has at least one populated).
//   - Tool input schemas render as a <pre><code>...</code></pre> JSON block.
//   - Header has a close button that calls `onClose()`.

export interface NodeInspectorProps {
  nodeId: string;
  onClose: () => void;
}

export function NodeInspector(props: NodeInspectorProps) {
  return (
    <div
      style={{
        width: 360,
        height: "100%",
        borderLeft: "1px solid var(--border)",
        background: "var(--bg-panel)",
        padding: 16,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>Inspector</h3>
        <button onClick={props.onClose}>×</button>
      </div>
      <p style={{ color: "var(--text-muted)" }}>
        NodeInspector stub — filled in by Phase 1 Agent 3. Selected: <code>{props.nodeId}</code>
      </p>
    </div>
  );
}
