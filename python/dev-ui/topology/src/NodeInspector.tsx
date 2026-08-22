// Right-side details panel for the topology UI.
//
// Fetches `/_apx/topology/inspect/{nodeId}` on mount and whenever `nodeId`
// changes. Supports Save (instructions) and Unwire (peer / factory tool)
// via the same Discover SSO write paths.

import { useEffect, useState } from "react";
import type {
  AgentDetails,
  InspectActions,
  InspectResponse,
  NodeType,
  ResourceDetails,
  SubAgentDetails,
  ToolDetails,
  ArtifactSummary,
  TopoNode,
  TopologyExecution,
} from "./types";
import {
  postUnwireAgent,
  postUnwireTool,
  saveInstructions,
} from "./wire";
import sampleTopology from "./sample-topology.json";

export interface NodeInspectorProps {
  nodeId: string;
  /** The selected node from the topology payload, including semantic metadata. */
  node?: TopoNode;
  execution?: TopologyExecution;
  artifactSummaries?: ArtifactSummary[];
  onClose: () => void;
  /** Called after a successful Save / Unwire so the graph can refresh. */
  onMutated?: (msg: string) => void;
  onError?: (msg: string) => void;
}

const panelStyle: React.CSSProperties = {
  width: 360,
  height: "100%",
  borderLeft: "1px solid var(--border)",
  background: "var(--bg-panel, var(--panel))",
  color: "var(--text)",
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  padding: "12px 16px",
  borderBottom: "1px solid var(--border)",
  gap: 8,
};

const headerTitleStyle: React.CSSProperties = {
  margin: 0,
  fontSize: 15,
  fontWeight: 600,
  whiteSpace: "nowrap",
  overflow: "hidden",
  textOverflow: "ellipsis",
};

const headerTypeStyle: React.CSSProperties = {
  fontSize: 11,
  color: "var(--muted, var(--text-muted))",
  textTransform: "uppercase",
  letterSpacing: 0.4,
  marginTop: 2,
};

const closeButtonStyle: React.CSSProperties = {
  flexShrink: 0,
  padding: "2px 8px",
  lineHeight: 1.2,
};

const bodyStyle: React.CSSProperties = {
  padding: 16,
  overflow: "auto",
  flex: 1,
};

const sectionStyle: React.CSSProperties = {
  marginBottom: 16,
};

const sectionTitleStyle: React.CSSProperties = {
  fontSize: 11,
  color: "var(--muted, var(--text-muted))",
  textTransform: "uppercase",
  letterSpacing: 0.4,
  marginBottom: 8,
  fontWeight: 600,
};

const dlStyle: React.CSSProperties = {
  margin: 0,
  display: "grid",
  gridTemplateColumns: "1fr",
  gap: 10,
};

const dtStyle: React.CSSProperties = {
  fontSize: 11,
  color: "var(--muted, var(--text-muted))",
  textTransform: "uppercase",
  letterSpacing: 0.3,
  marginBottom: 2,
};

const ddStyle: React.CSSProperties = {
  margin: 0,
  color: "var(--text)",
  fontSize: 13,
  wordBreak: "break-word",
};

const textareaStyle: React.CSSProperties = {
  width: "100%",
  minHeight: 140,
  resize: "vertical",
  font: "inherit",
  fontSize: 12,
  lineHeight: 1.4,
  color: "var(--text)",
  background: "var(--code-bg)",
  border: "1px solid var(--border)",
  borderRadius: 4,
  padding: 8,
  boxSizing: "border-box",
};

const actionsRowStyle: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
  marginTop: 12,
};

const dangerBtnStyle: React.CSSProperties = {
  font: "inherit",
  cursor: "pointer",
  color: "#fca5a5",
  background: "#2a1215",
  border: "1px solid #7f1d1d",
  borderRadius: 6,
  padding: "6px 12px",
};

const preStyle: React.CSSProperties = {
  margin: 0,
  maxHeight: 300,
  overflow: "auto",
  fontSize: 12,
};

const RESOURCE_KIND_LABELS: Record<ResourceDetails["resourceKind"], string> = {
  uc_function: "UC Function",
  genie_space: "Genie Space",
  vector_index: "Vector Index",
  serving_endpoint: "Serving Endpoint",
  sql_warehouse: "SQL Warehouse",
};

function resourceKindForNodeType(type: NodeType): ResourceDetails["resourceKind"] | null {
  switch (type) {
    case "UCFunction":
      return "uc_function";
    case "GenieSpace":
      return "genie_space";
    case "VectorIndex":
      return "vector_index";
    case "ServingEndpoint":
      return "serving_endpoint";
    case "WarehouseSQL":
      return "sql_warehouse";
    default:
      return null;
  }
}

function isAgentLikeType(type: NodeType): boolean {
  return (
    type === "Agent" ||
    type === "LlmAgent" ||
    type === "DataAgent" ||
    type === "SequentialAgent" ||
    type === "ParallelAgent" ||
    type === "LoopAgent" ||
    type === "RouterAgent" ||
    type === "HandoffAgent" ||
    type === "KeywordRouter"
  );
}

function buildFallback(nodeId: string): InspectResponse | null {
  const nodes = (sampleTopology as { nodes: TopoNode[] }).nodes;
  const node = nodes.find((n) => n.id === nodeId);
  if (!node) return null;

  const base: InspectResponse = {
    id: node.id,
    type: node.type,
    label: node.label,
    description: node.description,
  };

  if (isAgentLikeType(node.type)) {
    const agent: AgentDetails = {
      className: node.type,
      instructions:
        node.description ??
        "You are a helpful assistant. Answer the user's question using the tools available.",
      model: "databricks-claude-sonnet-4-6",
      toolCount: 2,
      subAgentCount: node.id === "agent:root" ? 3 : 0,
      maxIterations: node.type === "LoopAgent" ? 10 : undefined,
    };
    return {
      ...base,
      agent,
      actions: {
        canEditInstructions: true,
        canUnwire: false,
        wireTarget: "agent",
      },
    };
  }

  const resourceKind = resourceKindForNodeType(node.type);
  if (resourceKind) {
    return {
      ...base,
      resource: {
        resourceKind,
        identifier: node.label,
      },
      actions: { canEditInstructions: false, canUnwire: false },
    };
  }

  if (node.type === "SubAgent") {
    const subAgent: SubAgentDetails = {
      url: "https://remote.example.com/agent",
      cardSource: "well-known",
      resolvedName: node.label,
      ref: "$APX_PEER_EXAMPLE_URL",
    };
    return {
      ...base,
      subAgent,
      actions: {
        canEditInstructions: false,
        canUnwire: true,
        wireTarget: "agent",
        unwire: { kind: "agent", target: "agent", ref: "$APX_PEER_EXAMPLE_URL" },
      },
    };
  }

  const tool: ToolDetails = {
    name: node.label,
    description: node.description,
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "The query to run." },
      },
      required: ["query"],
    },
    isSync: true,
    hasObOTokenDep: false,
  };
  return { ...base, tool, actions: { canEditInstructions: false, canUnwire: false } };
}

export function NodeInspector(props: NodeInspectorProps) {
  const {
    nodeId,
    node,
    execution,
    artifactSummaries,
    onClose,
    onMutated,
    onError,
  } = props;
  const [data, setData] = useState<InspectResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [instrDraft, setInstrDraft] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);

    const url = `/_apx/topology/inspect/${encodeURIComponent(nodeId)}`;
    fetch(url)
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return (await res.json()) as InspectResponse;
      })
      .then((json) => {
        if (cancelled) return;
        setData(json);
        setInstrDraft(json.agent?.instructions || "");
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        if (import.meta.env.DEV) {
          const fallback = buildFallback(nodeId);
          if (fallback) {
            setData(fallback);
            setInstrDraft(fallback.agent?.instructions || "");
            setLoading(false);
            return;
          }
        }
        setError(message);
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  const onSaveInstructions = async () => {
    const text = instrDraft.trim();
    if (!text) {
      onError?.("Instructions cannot be empty.");
      return;
    }
    setBusy(true);
    try {
      const result = await saveInstructions(text);
      if (!result.ok) {
        onError?.(result.detail || result.error || "Save failed");
        return;
      }
      onMutated?.("Instructions saved — live in Chat.");
    } catch (err: unknown) {
      onError?.(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const onUnwire = async (actions: InspectActions) => {
    const uw = actions.unwire;
    if (!uw) return;
    const label =
      uw.kind === "agent"
        ? uw.ref || data?.label || "peer"
        : uw.binding_name || data?.label || "tool";
    if (!window.confirm(`Unwire ${label} from ${uw.target}?`)) return;
    setBusy(true);
    try {
      const result =
        uw.kind === "agent"
          ? await postUnwireAgent({ target: uw.target, ref: uw.ref || "" })
          : await postUnwireTool({
              target: uw.target,
              binding_name: uw.binding_name || "",
            });
      if (!result.ok) {
        onError?.(result.detail || result.error || "Unwire failed");
        return;
      }
      const live = result.applied_live
        ? "Removed live — no deploy needed."
        : "Removed from agent.py — restart if Chat still sees it.";
      onMutated?.(`Unwired ${label}. ${live}`);
      onClose();
    } catch (err: unknown) {
      onError?.(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <h3 style={headerTitleStyle} title={data?.label ?? nodeId}>
            {data?.label ?? nodeId}
          </h3>
          {data && <div style={headerTypeStyle}>{data.type}</div>}
        </div>
        <button onClick={onClose} style={closeButtonStyle} aria-label="Close inspector">
          ×
        </button>
      </div>

      <div style={bodyStyle}>
        {loading && <div style={{ color: "var(--muted)" }}>Loading…</div>}

        {error && !loading && (
          <div style={{ color: "#f87171" }}>Could not load node details: {error}</div>
        )}

        {data && !loading && !error && (
          <InspectBody
            data={data}
            node={node}
            execution={execution}
            artifactSummaries={artifactSummaries}
            instrDraft={instrDraft}
            setInstrDraft={setInstrDraft}
            busy={busy}
            onSaveInstructions={onSaveInstructions}
            onUnwire={onUnwire}
          />
        )}
      </div>
    </div>
  );
}

function InspectBody({
  data,
  node,
  execution,
  artifactSummaries,
  instrDraft,
  setInstrDraft,
  busy,
  onSaveInstructions,
  onUnwire,
}: {
  data: InspectResponse;
  node?: TopoNode;
  execution?: TopologyExecution;
  artifactSummaries?: ArtifactSummary[];
  instrDraft: string;
  setInstrDraft: (v: string) => void;
  busy: boolean;
  onSaveInstructions: () => void;
  onUnwire: (actions: InspectActions) => void;
}) {
  const actions = data.actions;
  const metadata = node?.metadata;
  const artifacts = artifactSummaries?.filter(
    (summary) => summary.source_agent === node?.id,
  );
  const runState = execution
    ? execution.failed_node_ids?.includes(node?.id || "")
      ? "failed"
      : execution.active_node_ids?.includes(node?.id || "")
        ? "active"
        : "not active"
    : null;
  return (
    <>
      {data.description && !data.agent && (
        <section style={sectionStyle}>
          <div style={sectionTitleStyle}>Description</div>
          <div style={ddStyle}>{data.description}</div>
        </section>
      )}

      <section style={sectionStyle}>
        <div style={sectionTitleStyle}>Identity</div>
        <dl style={dlStyle}>
          <Field label="ID">
            <code>{data.id}</code>
          </Field>
        </dl>
      </section>

      {(metadata && Object.keys(metadata).length > 0) || runState || artifacts?.length ? (
        <SemanticSection
          metadata={metadata}
          runState={runState}
          traceId={execution?.trace_id}
          artifacts={artifacts}
        />
      ) : null}

      {data.agent && (
        <AgentSection
          details={data.agent}
          canEdit={!!actions?.canEditInstructions}
          instrDraft={instrDraft}
          setInstrDraft={setInstrDraft}
          busy={busy}
          onSave={onSaveInstructions}
        />
      )}
      {data.tool && <ToolSection details={data.tool} />}
      {data.resource && <ResourceSection details={data.resource} />}
      {data.subAgent && <SubAgentSection details={data.subAgent} />}

      {actions?.canUnwire && actions.unwire && (
        <section style={sectionStyle}>
          <div style={sectionTitleStyle}>Edit</div>
          <div style={{ fontSize: 12, color: "var(--muted)", marginBottom: 8 }}>
            Remove this {actions.unwire.kind === "agent" ? "peer" : "tool"} from{" "}
            <code>{actions.unwire.target}</code> (writes agent.py + hot-applies when
            possible).
          </div>
          <div style={actionsRowStyle}>
            <button
              type="button"
              style={dangerBtnStyle}
              disabled={busy}
              onClick={() => onUnwire(actions)}
            >
              {busy ? "Working…" : "Unwire"}
            </button>
          </div>
        </section>
      )}
    </>
  );
}

function SemanticSection({
  metadata,
  runState,
  traceId,
  artifacts,
}: {
  metadata?: Record<string, unknown>;
  runState: string | null;
  traceId?: string;
  artifacts?: ArtifactSummary[];
}) {
  return (
    <section style={sectionStyle}>
      <div style={sectionTitleStyle}>Semantic overlay</div>
      <dl style={dlStyle}>
        {runState && <Field label="Run state">{runState}</Field>}
        {traceId && (
          <Field label="Trace">
            <code>{traceId}</code>
          </Field>
        )}
        {Object.entries(metadata || {}).map(([key, value]) => (
          <Field key={key} label={humanize(key)}>
            {formatOverlayValue(value)}
          </Field>
        ))}
        {artifacts && artifacts.length > 0 && (
          <Field label="Artifacts">
            <div style={{ display: "grid", gap: 6 }}>
              {artifacts.map((artifact, index) => (
                <div key={`${artifact.contract || "artifact"}-${index}`}>
                  <code>{artifact.contract || "artifact"}</code>
                  {Object.entries(artifact)
                    .filter(([key]) => key !== "source_agent" && key !== "contract")
                    .map(([key, value]) => (
                      <div key={key} style={{ color: "var(--muted)", marginTop: 2 }}>
                        {humanize(key)}: {formatOverlayValue(value)}
                      </div>
                    ))}
                </div>
              ))}
            </div>
          </Field>
        )}
      </dl>
    </section>
  );
}

function humanize(key: string): string {
  return key
    .replace(/([a-z])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .replace(/^./, (char) => char.toUpperCase());
}

function formatOverlayValue(value: unknown): React.ReactNode {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) return value.map(String).join(", ");
  return <code>{JSON.stringify(value)}</code>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt style={dtStyle}>{label}</dt>
      <dd style={ddStyle}>{children}</dd>
    </div>
  );
}

function AgentSection({
  details,
  canEdit,
  instrDraft,
  setInstrDraft,
  busy,
  onSave,
}: {
  details: AgentDetails;
  canEdit: boolean;
  instrDraft: string;
  setInstrDraft: (v: string) => void;
  busy: boolean;
  onSave: () => void;
}) {
  const dirty = canEdit && instrDraft !== (details.instructions || "");
  return (
    <section style={sectionStyle}>
      <div style={sectionTitleStyle}>Agent</div>
      <dl style={dlStyle}>
        <Field label="Class">
          <code>{details.className}</code>
        </Field>
        {details.model && (
          <Field label="Model">
            <code>{details.model}</code>
          </Field>
        )}
        <Field label="Tools">{details.toolCount}</Field>
        <Field label="Sub-agents">{details.subAgentCount}</Field>
        {details.maxIterations !== undefined && (
          <Field label="Max iterations">{details.maxIterations}</Field>
        )}
        <div>
          <dt style={dtStyle}>Instructions</dt>
          <dd style={ddStyle}>
            {canEdit ? (
              <>
                <textarea
                  style={textareaStyle}
                  value={instrDraft}
                  onChange={(e) => setInstrDraft(e.target.value)}
                  spellCheck={false}
                />
                <div style={actionsRowStyle}>
                  <button
                    type="button"
                    className="apx-btn"
                    disabled={busy || !dirty}
                    onClick={onSave}
                  >
                    {busy ? "Saving…" : "Save"}
                  </button>
                </div>
              </>
            ) : (
              <div
                style={{
                  ...ddStyle,
                  whiteSpace: "pre-wrap",
                  maxHeight: 200,
                  overflow: "auto",
                  background: "var(--code-bg)",
                  border: "1px solid var(--border)",
                  borderRadius: 4,
                  padding: 8,
                  fontSize: 12,
                }}
              >
                {details.instructions || "(none)"}
              </div>
            )}
          </dd>
        </div>
      </dl>
    </section>
  );
}

function ToolSection({ details }: { details: ToolDetails }) {
  return (
    <section style={sectionStyle}>
      <div style={sectionTitleStyle}>Tool</div>
      <dl style={dlStyle}>
        <Field label="Name">
          <code>{details.name}</code>
        </Field>
        {details.description && <Field label="Description">{details.description}</Field>}
        <Field label="Sync">{details.isSync ? "Yes" : "No"}</Field>
        <Field label="On-behalf-of token">{details.hasObOTokenDep ? "Yes" : "No"}</Field>
        {details.inputSchema && (
          <div>
            <dt style={dtStyle}>Input schema</dt>
            <dd style={ddStyle}>
              <pre style={preStyle}>
                <code>{JSON.stringify(details.inputSchema, null, 2)}</code>
              </pre>
            </dd>
          </div>
        )}
      </dl>
    </section>
  );
}

function ResourceSection({ details }: { details: ResourceDetails }) {
  return (
    <section style={sectionStyle}>
      <div style={sectionTitleStyle}>Resource</div>
      <dl style={dlStyle}>
        <Field label="Kind">{RESOURCE_KIND_LABELS[details.resourceKind]}</Field>
        <Field label="Identifier">
          <code>{details.identifier}</code>
        </Field>
        {details.url && (
          <Field label="URL">
            <a href={details.url} target="_blank" rel="noreferrer">
              {details.url}
            </a>
          </Field>
        )}
      </dl>
    </section>
  );
}

function SubAgentSection({ details }: { details: SubAgentDetails }) {
  return (
    <section style={sectionStyle}>
      <div style={sectionTitleStyle}>Sub-agent</div>
      <dl style={dlStyle}>
        {details.resolvedName && (
          <Field label="Resolved name">
            <code>{details.resolvedName}</code>
          </Field>
        )}
        {details.ref && (
          <Field label="Ref">
            <code>{details.ref}</code>
          </Field>
        )}
        {details.url && details.url !== details.ref && (
          <Field label="URL">
            <a href={details.url} target="_blank" rel="noreferrer">
              {details.url}
            </a>
          </Field>
        )}
        {details.cardSource && <Field label="Card source">{details.cardSource}</Field>}
      </dl>
    </section>
  );
}
