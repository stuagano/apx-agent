// Selected asset: properties, connections (click to navigate), Impact toggle.
// Trimmed port of Lattice DetailPanel — no annotations, multi-select, or cost.
import type { GraphEdge, GraphNode } from "./types";
import { NODE_COLORS } from "./types";

export interface DetailPanelProps {
  node: GraphNode;
  edges: GraphEdge[];
  byId: ReadonlyMap<string, GraphNode>;
  impactCount: number | null;
  impactError: string | null;
  onSelect: (id: string) => void;
  onImpact: () => void;
  onClearImpact: () => void;
  onClose: () => void;
}

function Prop({ k, v }: { k: string; v: unknown }) {
  if (v === null || v === undefined || v === "") return null;
  if (k === "columns" && Array.isArray(v)) {
    return (
      <div className="apx-dg-prop">
        <div className="apx-dg-prop-k">columns ({v.length})</div>
        <ul className="apx-dg-cols">
          {v.map((c) => {
            const col = c as { name: string; type: string };
            return <li key={col.name}><code>{col.name}</code> <span className="apx-dg-muted">{col.type}</span></li>;
          })}
        </ul>
      </div>
    );
  }
  return (
    <div className="apx-dg-prop">
      <div className="apx-dg-prop-k">{k}</div>
      <div className="apx-dg-prop-v">{typeof v === "object" ? JSON.stringify(v) : String(v)}</div>
    </div>
  );
}

export function DetailPanel(p: DetailPanelProps) {
  const out = p.edges.filter((e) => e.src === p.node.id);
  const inc = p.edges.filter((e) => e.dst === p.node.id);
  const link = (id: string, type: string) => {
    const n = p.byId.get(id);
    return (
      <li key={`${type}-${id}`}>
        <span className="apx-dg-muted">{type}</span>{" "}
        <button type="button" className="apx-dg-link" onClick={() => p.onSelect(id)}>{n?.name ?? id}</button>
      </li>
    );
  };
  return (
    <aside className="apx-dg-detail">
      <div className="apx-palette-head">
        <div>
          <div className="apx-palette-title" style={{ color: NODE_COLORS[p.node.type] }}>{p.node.type}</div>
          <div className="apx-dg-name-lg">{p.node.name}</div>
          <code className="apx-dg-muted">{p.node.fqn}</code>
        </div>
        <button type="button" className="apx-btn" aria-label="Close" onClick={p.onClose}>×</button>
      </div>
      <div className="apx-palette-actions">
        {p.impactCount === null ? (
          <button type="button" className="apx-btn" onClick={p.onImpact}>Impact</button>
        ) : (
          <button type="button" className="apx-btn" onClick={p.onClearImpact}>
            Clear impact ({p.impactCount} downstream)
          </button>
        )}
      </div>
      {p.impactError && <div className="apx-dg-warnings" role="alert">Impact failed: {p.impactError}</div>}
      {Object.entries(p.node.props).map(([k, v]) => <Prop key={k} k={k} v={v} />)}
      {out.length > 0 && (<><div className="apx-palette-sub">Outgoing</div><ul className="apx-dg-conns">{out.map((e) => link(e.dst, e.type))}</ul></>)}
      {inc.length > 0 && (<><div className="apx-palette-sub">Incoming</div><ul className="apx-dg-conns">{inc.map((e) => link(e.src, e.type))}</ul></>)}
    </aside>
  );
}
