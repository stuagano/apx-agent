// "This agent" — what the running agent is and uses, beside the workspace graph.
// Links and runtime facts come from /_apx/workspace-context; the footprint and the
// declared resources the graph doesn't hold come from graph.json's agent overlay.
import type { AgentContext, AgentOverlay } from "./types";

export interface AgentPanelProps {
  overlay: AgentOverlay | null;
  context: AgentContext | null;
  onlyAgent: boolean;
  onToggleOnly: () => void;
  onSelectAgent: () => void;
}

export function AgentPanel({ overlay, context, onlyAgent, onToggleOnly, onSelectAgent }: AgentPanelProps) {
  const facts: ReadonlyArray<[string, string | null]> = [
    ["Model", context?.model ?? null],
    ["Session store", context?.session_store ?? null],
    ["Memory store", context?.memory_store ?? null],
  ];
  return (
    <section className="apx-dg-agent" aria-label="This agent">
      <div className="apx-palette-title">This agent</div>
      {(context?.app_url || context?.experiment_url) && (
        <div className="apx-dg-agent-links">
          {context?.app_url && <a href={context.app_url} target="_blank" rel="noreferrer">Databricks App ↗</a>}
          {context?.experiment_url && <a href={context.experiment_url} target="_blank" rel="noreferrer">MLflow experiment ↗</a>}
        </div>
      )}
      {facts.map(([k, v]) => (
        <div key={k} className="apx-dg-prop">
          <div className="apx-dg-prop-k">{k}</div>
          <div className="apx-dg-prop-v">{v ?? <span className="apx-dg-muted">not bound</span>}</div>
        </div>
      ))}
      {overlay && (
        <>
          <button type="button" className="apx-btn" onClick={onSelectAgent}>
            Uses {overlay.footprint.length} asset{overlay.footprint.length === 1 ? "" : "s"} in this graph
          </button>
          <label className="apx-dg-filter">
            <input type="checkbox" checked={onlyAgent} onChange={onToggleOnly} /> Only this agent's assets
          </label>
          {overlay.unplaced.length > 0 && (
            <details className="apx-dg-legend">
              <summary>Not in this graph ({overlay.unplaced.length})</summary>
              <div className="apx-dg-muted">Outside the loaded catalogs, or a kind the graph doesn't draw.</div>
              <ul className="apx-dg-cols">
                {overlay.unplaced.map((r) => (
                  <li key={`${r.kind}:${r.identifier}`}><code>{r.kind}</code> {r.identifier}</li>
                ))}
              </ul>
            </details>
          )}
        </>
      )}
    </section>
  );
}
