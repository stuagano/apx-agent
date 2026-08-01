// App shell for /_apx/topology — owns selection state, fetches the topology JSON,
// and wires together <TopologyGraph />, <WirePalette />, and <NodeInspector />.
//
// Chrome matches the shared Dev UI nav (_ui_nav.py): APX badge + text tabs.
// Drag-from-Discover rail → drop on leaf Agent → POST wire API → refresh.
// Tracing badge + last-turn route highlight from MLflow / ring buffer.

import { useCallback, useEffect, useMemo, useState } from "react";
import { TopologyGraph } from "./TopologyGraph";
import { NodeInspector } from "./NodeInspector";
import { WirePalette } from "./WirePalette";
import { ChatDock } from "./ChatDock";
import type { TopologyResponse } from "./types";
import {
  fetchDiscoverTargets,
  isLeafAgentType,
  postWire,
  topologyNodeToWireTarget,
  type WirePayload,
} from "./wire";
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

/** Keep aligned with APX_NAV_PAGES in _ui_nav.py (no emoji chrome). */
const NAV_LINKS: ReadonlyArray<{ href: string; label: string; slug: string }> = [
  { href: "/_apx/agent", label: "Chat", slug: "agent" },
  { href: "/_apx/edit", label: "Edit", slug: "edit" },
  { href: "/_apx/eval", label: "Eval", slug: "eval" },
  { href: "/_apx/discover", label: "Discover", slug: "discover" },
  { href: "/_apx/grounding", label: "Grounding", slug: "grounding" },
  { href: "/_apx/probe", label: "Probe", slug: "probe" },
  { href: "/_apx/topology", label: "Topology", slug: "topology" },
];

const EMBED =
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).get("embed") === "1";

const ROUTE_POLL_MS = 4000;

export default function App() {
  const [state, setState] = useState<AppState>({
    data: null,
    loading: true,
    error: null,
    selected: null,
  });
  const [targets, setTargets] = useState<
    Array<{ name: string; eligible: boolean; reason?: string | null }>
  >([]);
  const [toast, setToast] = useState<Toast | null>(null);
  const [wiring, setWiring] = useState(false);
  const [ucContext, setUcContext] = useState<{ catalog?: string; schema?: string }>(
    {},
  );
  const [tracing, setTracing] = useState<TracingInfo | null>(null);
  const [route, setRoute] = useState<LastRoute | null>(null);
  const [tracingOpen, setTracingOpen] = useState(false);
  const [expDraft, setExpDraft] = useState("");
  const [savingExp, setSavingExp] = useState(false);
  const [chatOpen, setChatOpen] = useState(!EMBED);

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

  const loadTargets = useCallback(() => {
    fetchDiscoverTargets()
      .then((list) => setTargets(list.map((t) => ({
        name: t.name,
        eligible: t.eligible,
        reason: t.reason,
      }))))
      .catch(() => setTargets([]));
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
    loadTargets();
    loadTracing();
    loadRoute();
    fetch("/_apx/workspace-context")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) return;
        const catalog = (d.used_catalogs || [])[0] as string | undefined;
        const sch = (d.used_schemas || [])[0] as string | undefined;
        const schema =
          sch && sch.includes(".") ? sch.split(".")[1] : sch;
        setUcContext({ catalog, schema });
      })
      .catch(() => undefined);
  }, [load, loadTargets, loadTracing, loadRoute]);

  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") loadRoute();
    }, ROUTE_POLL_MS);
    return () => window.clearInterval(id);
  }, [loadRoute]);

  const eligibleTargets = useMemo(
    () => targets.filter((t) => t.eligible).map((t) => t.name),
    [targets],
  );

  const droppableIds = useMemo(() => {
    const ids = new Set<string>();
    const data = state.data;
    if (!data) return ids;
    for (const n of data.nodes) {
      if (!isLeafAgentType(n.type)) continue;
      if (topologyNodeToWireTarget(n.id, eligibleTargets)) {
        ids.add(n.id);
      }
    }
    return ids;
  }, [state.data, eligibleTargets]);

  const routeNodeIds = useMemo(
    () => new Set(route?.node_ids || []),
    [route],
  );
  const routeEdgeIds = useMemo(
    () => new Set(route?.edge_ids || []),
    [route],
  );

  const showToast = (text: string, ok: boolean) => {
    setToast({ text, ok });
    window.setTimeout(() => setToast(null), 6000);
  };

  const onWireDrop = async (nodeId: string, payload: WirePayload) => {
    if (!nodeId || !droppableIds.has(nodeId)) {
      const blocked = targets.find((t) => !t.eligible);
      const hint = blocked?.reason
        ? blocked.reason
        : "Drop onto a leaf Agent node (green dashed outline).";
      showToast(hint, false);
      return;
    }
    const target = topologyNodeToWireTarget(nodeId, eligibleTargets);
    if (!target) {
      showToast("Drop on a leaf Agent that has an agent.py assignment.", false);
      return;
    }
    setWiring(true);
    try {
      const result = await postWire(payload, target);
      if (!result.ok) {
        showToast(result.detail || result.error || "Wire failed", false);
        return;
      }
      const label = result.ref || result.binding_name || "item";
      const live = result.applied_live
        ? "Live in Chat — no deploy needed."
        : "Saved to agent.py — restart if Chat does not pick it up.";
      showToast(`Wired ${label} → ${target}. ${live}`, true);
      load();
      loadTargets();
    } catch (err: unknown) {
      showToast(err instanceof Error ? err.message : String(err), false);
    } finally {
      setWiring(false);
    }
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

  const data = state.data;
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
              {wiring ? " · wiring…" : ""}
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
                  loadTargets();
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
              Drag from the Discover rail onto a leaf Agent, or wire from{" "}
              <a href="/_apx/discover">Discover</a> /{" "}
              <a href="/_apx/edit">Edit</a>.
            </div>
            <button type="button" className="apx-btn" onClick={load}>
              Refresh
            </button>
          </div>
        ) : (
          <div className="apx-content">
            {!EMBED && (
              <WirePalette catalog={ucContext.catalog} schema={ucContext.schema} />
            )}
            <div className="apx-graph">
              <TopologyGraph
                data={data}
                selected={state.selected}
                routeNodeIds={routeNodeIds}
                routeEdgeIds={routeEdgeIds}
                droppableIds={droppableIds}
                showMap={data.nodes.length >= 12}
                onNodeClick={(id) => setState((s) => ({ ...s, selected: id }))}
                onWireDrop={EMBED ? undefined : onWireDrop}
              />
            </div>
            {state.selected && (
              <NodeInspector
                nodeId={state.selected}
                onClose={() => setState((s) => ({ ...s, selected: null }))}
                onMutated={(msg) => {
                  showToast(msg, true);
                  load();
                  loadTargets();
                }}
                onError={(msg) => showToast(msg, false)}
              />
            )}
            {!EMBED && (
              <ChatDock
                collapsed={!chatOpen}
                onToggle={() => setChatOpen((v) => !v)}
                onTurnComplete={() => {
                  loadRoute();
                  // Second pass — ring buffer may lag a tick behind stream end.
                  window.setTimeout(() => loadRoute(), 1200);
                }}
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
