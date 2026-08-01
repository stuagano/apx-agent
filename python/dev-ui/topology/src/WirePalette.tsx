// Left-rail Discover catalog — drag cards onto leaf Agent nodes to wire.
// Visual language matches /_apx/discover (cards + pills + accent buttons).

import { useCallback, useEffect, useState, type DragEvent } from "react";
import { WIRE_MIME, type WirePayload } from "./wire";

interface PaletteItem {
  id: string;
  label: string;
  hint: string;
  pill: "app" | "uc" | "genie_space" | "vector_search_index";
  pillLabel: string;
  payload: WirePayload;
}

interface WorkspaceAgent {
  name: string;
  source: string;
  url?: string | null;
  app_name?: string | null;
  description?: string | null;
}

interface WorkspaceFunction {
  full_name: string;
  comment?: string | null;
}

interface WorkspaceApi {
  kind: string;
  name: string;
  description?: string | null;
  mcp_url?: string | null;
  extra?: { space_id?: string; columns?: string[] } | null;
}

export interface WirePaletteProps {
  catalog?: string;
  schema?: string;
  collapsed?: boolean;
}

/** Fallback when the agent has no grounded UC resources (e.g. ungrounded DataAgent). */
const DEFAULT_UC = { catalog: "samples", schema: "nyctaxi" };

function cardDragStart(e: DragEvent, payload: WirePayload) {
  const raw = JSON.stringify(payload);
  e.dataTransfer.setData(WIRE_MIME, raw);
  // text/plain fallback — some browsers refuse custom MIME-only drags
  e.dataTransfer.setData("text/plain", raw);
  e.dataTransfer.effectAllowed = "copy";
}

export function WirePalette(props: WirePaletteProps) {
  const [items, setItems] = useState<PaletteItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(!!props.collapsed);
  const [hint, setHint] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setHint(null);
    const catalog = props.catalog || DEFAULT_UC.catalog;
    const schema = props.schema || DEFAULT_UC.schema;

    Promise.all([
      fetch("/_apx/workspace-agents").then((r) => (r.ok ? r.json() : { agents: [] })),
      fetch("/_apx/workspace-apis").then((r) => (r.ok ? r.json() : { apis: [] })),
      fetch(
        `/_apx/workspace-functions?${new URLSearchParams({ catalog, schema })}`,
      ).then((r) => (r.ok ? r.json() : { functions: [] })),
    ])
      .then(([agentsBody, apisBody, fnsBody]) => {
        const next: PaletteItem[] = [];
        let agentSkippedNoUrl = 0;
        for (const a of (agentsBody.agents || []) as WorkspaceAgent[]) {
          if (!a.url) {
            agentSkippedNoUrl += 1;
            continue;
          }
          next.push({
            id: `agent:${a.url}`,
            label: a.name,
            hint: a.description || a.app_name || a.source,
            pill: "app",
            pillLabel: "app",
            payload: {
              wire: "agent",
              url: a.url,
              name: a.name,
              app_name: a.app_name || a.name,
            },
          });
        }
        for (const f of (fnsBody.functions || []) as WorkspaceFunction[]) {
          next.push({
            id: `uc:${f.full_name}`,
            label: f.full_name.split(".").pop() || f.full_name,
            hint: f.full_name,
            pill: "uc",
            pillLabel: "uc",
            payload: { wire: "uc_function", full_name: f.full_name },
          });
        }
        let servingOnly = 0;
        for (const api of (apisBody.apis || []) as WorkspaceApi[]) {
          if (api.kind === "genie_space") {
            const spaceId = api.extra?.space_id;
            if (!spaceId) continue;
            next.push({
              id: `genie:${spaceId}`,
              label: api.name,
              hint: api.description || "Genie space",
              pill: "genie_space",
              pillLabel: "genie",
              payload: {
                wire: "genie_space",
                space_id: spaceId,
                title: api.name,
              },
            });
          } else if (api.kind === "vector_search_index") {
            next.push({
              id: `vs:${api.name}`,
              label: api.name.split(".").pop() || api.name,
              hint: api.name,
              pill: "vector_search_index",
              pillLabel: "vector search",
              payload: {
                wire: "vector_search_index",
                index_name: api.name,
                columns: api.extra?.columns || ["content"],
              },
            });
          } else if (api.kind === "serving_endpoint") {
            servingOnly += 1;
          }
        }
        const notes: string[] = [];
        if (!(agentsBody.agents || []).length) {
          notes.push(
            "No Apps peers answered A2A yet. Peer discovery needs APX_DISCOVER_APP_URLS when apps.list is blocked for scoped OBO.",
          );
        } else if (agentSkippedNoUrl) {
          notes.push(`${agentSkippedNoUrl} UC-only agent(s) skipped (no Apps URL).`);
        }
        if (servingOnly && !next.some((i) => i.pill === "genie_space" || i.pill === "vector_search_index")) {
          notes.push(
            `${servingOnly} serving endpoint(s) found but not wireable as tools — use Genie / Vector Search / Apps peers.`,
          );
        }
        if (!props.catalog) {
          notes.push(`UC scan: ${catalog}.${schema} (default).`);
        }
        setHint(notes.length ? notes.join(" ") : null);
        setItems(next);
        setLoading(false);
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
  }, [props.catalog, props.schema]);

  useEffect(() => {
    load();
  }, [load]);

  if (collapsed) {
    return (
      <aside className="apx-palette collapsed">
        <button
          type="button"
          className="apx-btn"
          onClick={() => setCollapsed(false)}
          title="Show Discover wire list"
        >
          Discover
        </button>
      </aside>
    );
  }

  return (
    <aside className="apx-palette">
      <div className="apx-palette-head">
        <div>
          <div className="apx-palette-title">Discover</div>
          <div className="apx-palette-sub">
            Drag onto a leaf Agent to wire — same as{" "}
            <a href="/_apx/discover">/_apx/discover</a>
          </div>
        </div>
        <div className="apx-palette-actions">
          <button
            type="button"
            className="apx-btn"
            onClick={load}
            title="Refresh catalog"
            disabled={loading}
          >
            Refresh
          </button>
          <button
            type="button"
            className="apx-btn secondary"
            onClick={() => setCollapsed(true)}
            title="Collapse"
          >
            Hide
          </button>
        </div>
      </div>
      {loading ? (
        <div className="apx-palette-empty">Scanning workspace…</div>
      ) : error ? (
        <div className="apx-palette-empty err">{error}</div>
      ) : items.length === 0 ? (
        <div className="apx-palette-empty">
          Nothing wireable yet.
          {hint ? <> {hint}</> : null}{" "}
          Open <a href="/_apx/discover">Discover</a> to browse peers and pick a
          catalog.schema for UC functions.
        </div>
      ) : (
        <div className="apx-palette-list">
          {hint && <div className="apx-palette-empty" style={{ padding: "0 0 8px" }}>{hint}</div>}
          {items.map((item) => (
            <div
              key={item.id}
              className="apx-card apx-card-drag"
              draggable
              onDragStart={(e) => cardDragStart(e, item.payload)}
              title={`Drag to wire · ${item.hint}`}
            >
              <div className="apx-card-title">
                <span className={`apx-pill ${item.pill}`}>{item.pillLabel}</span>
                <span className="apx-card-label">{item.label}</span>
              </div>
              <div className="apx-card-meta">{item.hint}</div>
            </div>
          ))}
        </div>
      )}
    </aside>
  );
}
