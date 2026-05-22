// Right-side details panel for the topology UI.
//
// Fetches `/_apx/topology/inspect/{nodeId}` on mount and whenever `nodeId`
// changes. Renders an InspectResponse — one of agent / tool / resource /
// subAgent will be populated. Falls back to a derived inspect response from
// `sample-topology.json` in dev mode so the panel can be developed without a
// running backend.

import { useEffect, useState } from "react";
import type {
  AgentDetails,
  InspectResponse,
  NodeType,
  ResourceDetails,
  SubAgentDetails,
  ToolDetails,
  TopoNode,
} from "./types";
import sampleTopology from "./sample-topology.json";

export interface NodeInspectorProps {
  nodeId: string;
  onClose: () => void;
}

// --- styles -----------------------------------------------------------------

const panelStyle: React.CSSProperties = {
  width: 360,
  height: "100%",
  borderLeft: "1px solid var(--border)",
  background: "var(--bg-panel)",
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
  color: "var(--text-muted)",
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
  color: "var(--text-muted)",
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
  color: "var(--text-muted)",
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

const truncatedTextStyle: React.CSSProperties = {
  ...ddStyle,
  whiteSpace: "pre-wrap",
  maxHeight: 200,
  overflow: "auto",
  background: "var(--code-bg)",
  border: "1px solid var(--border)",
  borderRadius: 4,
  padding: 8,
  fontSize: 12,
};

const preStyle: React.CSSProperties = {
  margin: 0,
  maxHeight: 300,
  overflow: "auto",
  fontSize: 12,
};

// --- helpers ----------------------------------------------------------------

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
    type === "SequentialAgent" ||
    type === "ParallelAgent" ||
    type === "LoopAgent" ||
    type === "RouterAgent" ||
    type === "HandoffAgent"
  );
}

/**
 * Build a plausible InspectResponse from the bundled sample topology so the
 * panel can be developed and demoed without a backend.
 */
function buildFallback(nodeId: string): InspectResponse | null {
  const nodes = (sampleTopology as { nodes: TopoNode[] }).nodes;
  const node = nodes.find((n) => n.id === nodeId);
  if (!node) {
    return null;
  }

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
    return { ...base, agent };
  }

  const resourceKind = resourceKindForNodeType(node.type);
  if (resourceKind) {
    const resource: ResourceDetails = {
      resourceKind,
      identifier: node.label,
      url:
        resourceKind === "uc_function" || resourceKind === "vector_index"
          ? undefined
          : `https://example.cloud.databricks.com/${resourceKind}/${encodeURIComponent(node.label)}`,
    };
    return { ...base, resource };
  }

  if (node.type === "SubAgent") {
    const subAgent: SubAgentDetails = {
      url: "https://remote.example.com/agent",
      cardSource: "well-known",
      resolvedName: node.label,
    };
    return { ...base, subAgent };
  }

  // Default: a generic tool.
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
  return { ...base, tool };
}

// --- component --------------------------------------------------------------

export function NodeInspector(props: NodeInspectorProps) {
  const { nodeId, onClose } = props;
  const [data, setData] = useState<InspectResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);

    const url = `/_apx/topology/inspect/${encodeURIComponent(nodeId)}`;
    fetch(url)
      .then(async (res) => {
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        return (await res.json()) as InspectResponse;
      })
      .then((json) => {
        if (cancelled) return;
        setData(json);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        if (import.meta.env.DEV) {
          const fallback = buildFallback(nodeId);
          if (fallback) {
            setData(fallback);
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
        {loading && <div style={{ color: "var(--text-muted)" }}>Loading…</div>}

        {error && !loading && (
          <div style={{ color: "#f87171" }}>Could not load node details: {error}</div>
        )}

        {data && !loading && !error && <InspectBody data={data} />}
      </div>
    </div>
  );
}

// --- body renderer ----------------------------------------------------------

function InspectBody({ data }: { data: InspectResponse }) {
  return (
    <>
      {data.description && (
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

      {data.agent && <AgentSection details={data.agent} />}
      {data.tool && <ToolSection details={data.tool} />}
      {data.resource && <ResourceSection details={data.resource} />}
      {data.subAgent && <SubAgentSection details={data.subAgent} />}
    </>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt style={dtStyle}>{label}</dt>
      <dd style={ddStyle}>{children}</dd>
    </div>
  );
}

function AgentSection({ details }: { details: AgentDetails }) {
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
        {details.instructions && (
          <div>
            <dt style={dtStyle}>Instructions</dt>
            <dd style={truncatedTextStyle}>{details.instructions}</dd>
          </div>
        )}
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
        {details.url && (
          <Field label="URL">
            <a href={details.url} target="_blank" rel="noreferrer">
              {details.url}
            </a>
          </Field>
        )}
        {details.cardSource && <Field label="Card source">{details.cardSource}</Field>}
        {details.resolvedName && (
          <Field label="Resolved name">
            <code>{details.resolvedName}</code>
          </Field>
        )}
      </dl>
    </section>
  );
}
