// App shell for /_apx/topology — owns selection state, fetches the topology JSON,
// and wires together <TopologyGraph />, and <NodeInspector />.
//
// Chrome matches the shared Dev UI nav (_ui_nav.py): APX badge + text tabs.
// Tracing badge + last-turn route highlight from MLflow / ring buffer.

import { useCallback, useEffect, useMemo, useState } from "react";
import { TopologyGraph } from "./TopologyGraph";
import { NodeInspector } from "./NodeInspector";
import { NAV_LINKS } from "./nav";
import { ChatDock } from "./ChatDock";
import { WorkflowPanel } from "./WorkflowPanel";
import type { ExampleWorkflow, TopoNode, TopologyResponse } from "./types";
import {
  fetchLastRoute,
  fetchTracingInfo,
  setExperimentId,
  type LastRoute,
  type TracingInfo,
} from "./tracing";
import sampleData from "./sample-topology.json";

interface AppState {
  data: TopologyResponse | null;
  loading: boolean;
  error: string | null;
  selected: string | null;
}

interface Toast {
  text: string;
  ok: boolean;
}

const EMBED =
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).get("embed") === "1";

const ROUTE_POLL_MS = 4000;

type WorkflowStatus = "declared" | "active" | "partial" | "completed" | "failed";

interface ActiveWorkflowRun {
  workflowId: string;
  requestId: number;
}

function workflowStatus(
  workflow: ExampleWorkflow,
  observedNodeIds: ReadonlySet<string>,
  failed: boolean,
): WorkflowStatus {
  if (failed) return "failed";
  const observed = workflow.route.filter((stage) => observedNodeIds.has(stage));
  if (observed.length === 0) return "declared";
  if (observed.length === workflow.route.length) return "completed";
  return "partial";
}

function resolveWorkflowRoute(
  workflow: ExampleWorkflow,
  nodes: TopoNode[],
): Array<{ stage: string; nodeId?: string }> {
  return workflow.route.map((stage) => ({
    stage,
    nodeId: nodes.find((node) => node.id === stage)?.id ?? nodes.find(
      (node) => node.label.toLocaleLowerCase() === stage.toLocaleLowerCase(),
    )?.id,
  }));
}

export default function App() {
  const [state, setState] = useState<AppState>({
    data: null,
    loading: true,
    error: null,
    selected: null,
  });
  const [toast, setToast] = useState<Toast | null>(null);
  const [tracing, setTracing] = useState<TracingInfo | null>(null);
  const [route, setRoute] = useState<LastRoute | null>(null);
  const [tracingOpen, setTracingOpen] = useState(false);
  const [expDraft, setExpDraft] = useState("");
  const [savingExp, setSavingExp] = useState(false);
  const [chatOpen, setChatOpen] = useState(!EMBED);
  const [chatSending, setChatSending] = useState(false);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
  const [activeWorkflowRun, setActiveWorkflowRun] = useState<ActiveWorkflowRun | null>(null);
  const [nextWorkflowRequestId, setNextWorkflowRequestId] = useState(0);
  const [failedWorkflowIds, setFailedWorkflowIds] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const data = state.data;

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

  const loadTracing = useCallback(() => {
    fetchTracingInfo()
      .then((info) => {
        setTracing(info);
        if (info?.experiment_id) setExpDraft(info.experiment_id);
      })
      .catch(() => setTracing(null));
  }, []);

  const loadRoute = useCallback(() => {
    fetchLastRoute()
      .then((r) => {
        if (!r) return;
        setRoute((prev) => {
          if (
            prev &&
            prev.trace_id === r.trace_id &&
            prev.node_ids.length === r.node_ids.length &&
            prev.node_ids.every((id, i) => id === r.node_ids[i])
          ) {
            return prev;
          }
          return r;
        });
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    loadTracing();
    loadRoute();
  }, [load, loadTracing, loadRoute]);

  useEffect(() => {
    const workflows = state.data?.workflows ?? [];
    if (selectedWorkflowId && workflows.some((workflow) => workflow.id === selectedWorkflowId)) {
      return;
    }
    setSelectedWorkflowId(workflows[0]?.id ?? null);
  }, [selectedWorkflowId, state.data]);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") loadRoute();
    }, ROUTE_POLL_MS);
    return () => window.clearInterval(id);
  }, [loadRoute]);

  const observedNodeIds = useMemo(
    () => new Set([
      ...(route?.node_ids || []),
      ...(state.data?.execution?.active_node_ids || []),
    ]),
    [route, state.data],
  );
  const observedEdgeIds = useMemo(
    () => new Set([
      ...(route?.edge_ids || []),
      ...(state.data?.execution?.completed_edge_ids || []),
    ]),
    [route, state.data],
  );

  const selectedWorkflow = useMemo(
    () => data?.workflows?.find((workflow) => workflow.id === selectedWorkflowId),
    [data, selectedWorkflowId],
  );
  const resolvedSelectedRoute = useMemo(
    () => selectedWorkflow && data
      ? resolveWorkflowRoute(selectedWorkflow, data.nodes)
      : [],
    [data, selectedWorkflow],
  );
  const unresolvedRouteStages = useMemo(
    () => new Set(resolvedSelectedRoute.flatMap((stage) => stage.nodeId ? [] : [stage.stage])),
    [resolvedSelectedRoute],
  );
  // Graph highlights are evidence only: declared workflow routes stay in the rail.
  const routeNodeIds = observedNodeIds;
  const routeEdgeIds = observedEdgeIds;
  const workflowStatuses = useMemo(() => new Map((data?.workflows ?? []).map((workflow) => {
    const resolved = resolveWorkflowRoute(workflow, data?.nodes ?? []);
    const observedLogicalNodeIds = new Set(resolved.flatMap((stage) => stage.nodeId && observedNodeIds.has(stage.nodeId)
      ? [stage.stage]
      : []));
    return [
      workflow.id,
      workflowStatus(workflow, observedLogicalNodeIds, failedWorkflowIds.has(workflow.id)),
    ] as const;
  })), [data, failedWorkflowIds, observedNodeIds]);
  const observedRoute = useMemo(
    () => (route?.node_ids ?? []).map(
      (id) => data?.nodes.find((node) => node.id === id)?.label ?? id,
    ),
    [data, route],
  );

  const showToast = (text: string, ok: boolean) => {
    setToast({ text, ok });
    window.setTimeout(() => setToast(null), 6000);
  };

  const runWorkflow = (workflow: ExampleWorkflow) => {
    if (EMBED) return;
    if (chatSending || activeWorkflowRun) {
      showToast("Wait for the current Chat response before running an example.", false);
      return;
    }
    const requestId = nextWorkflowRequestId + 1;
    setSelectedWorkflowId(workflow.id);
    setActiveWorkflowRun({ workflowId: workflow.id, requestId });
    setNextWorkflowRequestId(requestId);
    setFailedWorkflowIds((failed) => {
      const next = new Set(failed);
      next.delete(workflow.id);
      return next;
    });
    setChatOpen(true);
  };

  const saveExperiment = async () => {
    const eid = expDraft.trim();
    if (!eid) {
      showToast("Enter an MLflow experiment id.", false);
      return;
    }
    setSavingExp(true);
    try {
      const result = await setExperimentId(eid);
      if (!result.ok) {
        showToast(result.detail || "Failed to set experiment", false);
        return;
      }
      if (result.info) setTracing({ ...result.info, configured: true });
      else loadTracing();
      showToast(`Traces → ${eid}`, true);
      setTracingOpen(false);
    } catch (err: unknown) {
      showToast(err instanceof Error ? err.message : String(err), false);
    } finally {
      setSavingExp(false);
    }
  };

  const nodeCount = data?.nodes.length ?? 0;

  const isEmbedded =
    typeof window !== "undefined" && window.self !== window.top;

  const expLabel =
    tracing?.experiment_name ||
    tracing?.experiment_id ||
    "not configured";

  return (
    <div className="apx-app">
      {!isEmbedded && (
        <div id="apx-header">
          <div id="apx-nav">
            <span className="badge">APX dev</span>
            <span className="apx-nav-meta">
              {nodeCount} {nodeCount === 1 ? "node" : "nodes"}
            </span>
            <button
              type="button"
              className={`apx-trace-badge${tracing?.configured ? "" : " muted"}`}
              onClick={() => setTracingOpen((v) => !v)}
              title="Tracing destination — click to configure"
            >
              traces → {expLabel}
            </button>
            <nav>
              <button
                type="button"
                className="apx-nav-refresh"
                onClick={() => {
                  load();
                  loadTracing();
                  loadRoute();
                }}
                disabled={state.loading}
                title="Re-fetch topology + last turn"
              >
                Refresh
              </button>
              <button
                type="button"
                className={`apx-nav-refresh${chatOpen ? " active" : ""}`}
                onClick={() => setChatOpen((v) => !v)}
                title="Toggle Chat dock"
              >
                Chat
              </button>
              {NAV_LINKS.filter((link) => link.slug !== "agent").map((link) => (
                <a
                  key={link.href}
                  href={link.href}
                  className={link.slug === "topology" ? "active" : undefined}
                >
                  {link.label}
                </a>
              ))}
            </nav>
          </div>
          {tracingOpen && (
            <div className="apx-tracing-bar">
              <label htmlFor="apx-exp-id">MLFLOW_EXPERIMENT_ID</label>
              <input
                id="apx-exp-id"
                value={expDraft}
                onChange={(e) => setExpDraft(e.target.value)}
                placeholder="numeric experiment id"
                spellCheck={false}
              />
              <button
                type="button"
                className="apx-btn"
                onClick={saveExperiment}
                disabled={savingExp}
              >
                {savingExp ? "Saving…" : "Save"}
              </button>
              {tracing?.experiment_url && (
                <a href={tracing.experiment_url} target="_blank" rel="noreferrer">
                  Open in workspace
                </a>
              )}
              <button
                type="button"
                className="apx-nav-refresh"
                onClick={() => setTracingOpen(false)}
              >
                Close
              </button>
            </div>
          )}
        </div>
      )}

      <main className={`apx-main ${isEmbedded ? "" : "with-nav"} ${tracingOpen ? "with-tracing" : ""}`}>
        {state.loading ? (
          <div className="apx-loading">
            <div className="apx-spinner" aria-hidden="true" />
            <div>Loading topology&hellip;</div>
          </div>
        ) : state.error || !data ? (
          <div className="apx-empty">
            <div className="apx-empty-title">Could not load topology</div>
            <div className="apx-empty-body">{state.error ?? "No data returned."}</div>
            <button type="button" className="apx-btn" onClick={load}>
              Try again
            </button>
          </div>
        ) : data.nodes.length === 0 ? (
          <div className="apx-empty">
            <div className="apx-empty-title">No tools or sub-agents yet</div>
            <div className="apx-empty-body">
              No tools or sub-agents yet. Add them in <a href="/_apx/edit">Edit</a>.
            </div>
            <button type="button" className="apx-btn" onClick={load}>
              Refresh
            </button>
          </div>
        ) : (
          <div className="apx-content">
            <div className="apx-workspace">
              <WorkflowPanel
                workflows={data.workflows ?? []}
                selectedWorkflowId={selectedWorkflowId}
                routeNodeIds={routeNodeIds}
                routeEdgeIds={routeEdgeIds}
                statuses={workflowStatuses}
                unresolvedRouteStages={unresolvedRouteStages}
                observedRoute={observedRoute}
                activeWorkflowId={activeWorkflowRun?.workflowId ?? null}
                canRun={!EMBED}
                senderBusy={chatSending}
                onSelect={setSelectedWorkflowId}
                onRun={runWorkflow}
              />
              <div className="apx-graph">
                <TopologyGraph
                  data={data}
                  selected={state.selected}
                  routeNodeIds={routeNodeIds}
                  routeEdgeIds={routeEdgeIds}
                  showMap={data.nodes.length >= 12}
                  onNodeClick={(id) => setState((s) => ({ ...s, selected: id }))}
                />
              </div>
            </div>
            {state.selected && (
              <NodeInspector
                nodeId={state.selected}
                node={data.nodes.find((node) => node.id === state.selected)}
                execution={data.execution}
                artifactSummaries={data.artifact_summaries}
                onClose={() => setState((s) => ({ ...s, selected: null }))}
                onMutated={(msg) => {
                  showToast(msg, true);
                  load();
                }}
                onError={(msg) => showToast(msg, false)}
              />
            )}
            {!EMBED && (
              <ChatDock
                collapsed={!chatOpen}
                onToggle={() => setChatOpen((v) => !v)}
                onSendingChange={setChatSending}
              />
            )}
          </div>
        )}
      </main>
      {toast && (
        <div className={`apx-toast ${toast.ok ? "ok" : "err"}`}>{toast.text}</div>
      )}
    </div>
  );
}
