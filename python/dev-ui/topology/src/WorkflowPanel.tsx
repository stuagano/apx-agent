import type { ExampleWorkflow } from "./types";

type WorkflowStatus = "declared" | "active" | "partial" | "completed" | "failed";

export interface WorkflowPanelProps {
  workflows: ExampleWorkflow[];
  selectedWorkflowId: string | null;
  routeNodeIds: ReadonlySet<string>;
  routeEdgeIds: ReadonlySet<string>;
  onSelect: (workflowId: string) => void;
  onRun: (workflow: ExampleWorkflow) => void;
  statuses: ReadonlyMap<string, WorkflowStatus>;
  unresolvedRouteStages: ReadonlySet<string>;
  observedRoute: string[];
  activeWorkflowId: string | null;
}

function statusText(status: WorkflowStatus): string {
  switch (status) {
    case "active":
      return "Running example";
    case "partial":
      return "Route partially observed";
    case "completed":
      return "Route observed";
    case "failed":
      return "Run failed";
    default:
      return "Declared route — not yet run";
  }
}

export function WorkflowPanel({
  workflows,
  selectedWorkflowId,
  routeNodeIds,
  routeEdgeIds,
  onSelect,
  onRun,
  statuses,
  unresolvedRouteStages,
  observedRoute,
  activeWorkflowId,
}: WorkflowPanelProps) {
  if (workflows.length === 0) return null;

  return (
    <section className="apx-workflows" aria-labelledby="apx-workflows-title">
      <div className="apx-workflows-head">
        <div>
          <h2 id="apx-workflows-title">Example workflows</h2>
          <p>Declared routes become observed only after a Chat run produces trace evidence.</p>
        </div>
        {selectedWorkflowId && (
          <span className="apx-workflows-graph-count">
            {routeNodeIds.size} nodes · {routeEdgeIds.size} edges highlighted
          </span>
        )}
      </div>
      <div className="apx-workflows-list">
        {workflows.map((workflow) => {
          const selected = workflow.id === selectedWorkflowId;
          const running = workflow.id === activeWorkflowId;
          const status = statuses.get(workflow.id) ?? "declared";
          return (
            <article
              key={workflow.id}
              className={`apx-workflow-card${selected ? " selected" : ""}`}
            >
              <div className="apx-workflow-title-row">
                <h3>{workflow.title}</h3>
                <span className={`apx-workflow-status ${status}`}>{statusText(status)}</span>
              </div>
              <p className="apx-workflow-question">{workflow.question}</p>
              <p className="apx-workflow-purpose">{workflow.purpose}</p>
              <ol className="apx-workflow-route" aria-label={`${workflow.title} route`}>
                {workflow.route.map((stage) => (
                  <li key={stage} className={selected && unresolvedRouteStages.has(stage) ? "unresolved" : undefined}>
                    {stage}
                  </li>
                ))}
              </ol>
              <p className="apx-workflow-outcome"><strong>Outcome:</strong> {workflow.outcome}</p>
              {selected && unresolvedRouteStages.size > 0 && (
                <div className="apx-workflow-route-note">
                  <span>Declared logical route: {workflow.route.join(" → ")}</span>
                  <span>Observed trace: {observedRoute.length ? observedRoute.join(" → ") : "not yet available"}</span>
                </div>
              )}
              <div className="apx-workflow-actions">
                <button
                  type="button"
                  className="apx-btn secondary"
                  aria-pressed={selected}
                  onClick={() => onSelect(workflow.id)}
                >
                  View route
                </button>
                <button
                  type="button"
                  className="apx-btn"
                  onClick={() => onRun(workflow)}
                  disabled={activeWorkflowId !== null}
                >
                  {running ? "Running…" : "Run example"}
                </button>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
