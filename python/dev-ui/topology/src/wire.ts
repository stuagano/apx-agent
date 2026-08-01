// Discover → Topology drag-to-wire helpers.
// Calls the same POST /_apx/discover/wire-* APIs as the Discover page.

export type WireKind = "agent" | "uc_function" | "genie_space" | "vector_search_index";

export interface DiscoverTarget {
  name: string;
  kind: string;
  eligible: boolean;
  reason?: string | null;
  sub_agents?: string[];
}

export interface WirePayload {
  wire: WireKind;
  // agent
  url?: string;
  name?: string;
  app_name?: string;
  // uc_function
  full_name?: string;
  // genie
  space_id?: string;
  title?: string;
  // vector search
  index_name?: string;
  columns?: string[];
}

export interface WireResult {
  ok: boolean;
  applied_live?: boolean;
  restart_required?: boolean;
  target?: string;
  ref?: string;
  binding_name?: string;
  already_present?: boolean;
  detail?: string;
  error?: string;
}

const LEAF_TYPES = new Set(["Agent", "LlmAgent", "DataAgent"]);

export function isLeafAgentType(type: string): boolean {
  return LEAF_TYPES.has(type);
}

/** Map topology node id → Discover wire target (Python assignment name). */
export function topologyNodeToWireTarget(
  nodeId: string,
  eligibleNames: string[],
): string | null {
  if (!eligibleNames.length) return null;
  if (nodeId === "agent:root") {
    if (eligibleNames.includes("agent")) return "agent";
    if (eligibleNames.length === 1) return eligibleNames[0]!;
    return null;
  }
  if (nodeId.startsWith("agent:root.")) {
    const last = nodeId.slice("agent:root.".length).split(".").pop() || "";
    if (eligibleNames.includes(last)) return last;
  }
  return null;
}

function readToken(): string {
  try {
    const q = new URLSearchParams(window.location.search).get("token");
    if (q) {
      localStorage.setItem("apxDevToken", q);
      return q;
    }
  } catch {
    /* ignore */
  }
  try {
    return localStorage.getItem("apxDevToken") || "";
  } catch {
    return "";
  }
}

async function apxDevFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers || {});
  const token = readToken();
  if (token) headers.set("X-APX-Dev-Token", token);
  // Deployed Apps authorize writes via SSO (X-Forwarded-Access-Token from the
  // proxy). Optional X-APX-Dev-Token is only for automation overrides.
  return fetch(url, { ...init, headers });
}

export async function fetchDiscoverTargets(): Promise<DiscoverTarget[]> {
  const r = await fetch("/_apx/discover/targets");
  if (!r.ok) return [];
  const d = (await r.json()) as { targets?: DiscoverTarget[] };
  return d.targets || [];
}

export async function postWire(
  payload: WirePayload,
  target: string,
): Promise<WireResult> {
  if (payload.wire === "agent") {
    const r = await apxDevFetch("/_apx/discover/wire-agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: payload.url,
        name: payload.name,
        app_name: payload.app_name || payload.name,
        target,
        use_env: true,
      }),
    });
    const d = (await r.json().catch(() => ({}))) as WireResult;
    if (!r.ok) {
      return {
        ok: false,
        detail: typeof d.detail === "string" ? d.detail : d.error || `HTTP ${r.status}`,
      };
    }
    return { ...d, ok: true };
  }

  const body: Record<string, unknown> = { target, kind: payload.wire };
  if (payload.wire === "uc_function") body.full_name = payload.full_name;
  if (payload.wire === "genie_space") {
    body.space_id = payload.space_id;
    body.title = payload.title;
  }
  if (payload.wire === "vector_search_index") {
    body.index_name = payload.index_name;
    body.columns = payload.columns || ["content"];
  }

  const r = await apxDevFetch("/_apx/discover/wire-tool", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const d = (await r.json().catch(() => ({}))) as WireResult;
  if (!r.ok) {
    return {
      ok: false,
      detail: typeof d.detail === "string" ? d.detail : d.error || `HTTP ${r.status}`,
    };
  }
  return { ...d, ok: true };
}

export async function postUnwireAgent(opts: {
  target: string;
  ref: string;
}): Promise<WireResult> {
  const r = await apxDevFetch("/_apx/discover/unwire-agent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target: opts.target,
      ref: opts.ref,
    }),
  });
  const d = (await r.json().catch(() => ({}))) as WireResult;
  if (!r.ok) {
    return {
      ok: false,
      detail: typeof d.detail === "string" ? d.detail : d.error || `HTTP ${r.status}`,
    };
  }
  return { ...d, ok: true };
}

export async function postUnwireTool(opts: {
  target: string;
  binding_name: string;
}): Promise<WireResult> {
  const r = await apxDevFetch("/_apx/discover/unwire-tool", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target: opts.target,
      binding_name: opts.binding_name,
      kind: "uc_function",
    }),
  });
  const d = (await r.json().catch(() => ({}))) as WireResult;
  if (!r.ok) {
    return {
      ok: false,
      detail: typeof d.detail === "string" ? d.detail : d.error || `HTTP ${r.status}`,
    };
  }
  return { ...d, ok: true };
}

export async function saveInstructions(instructions: string): Promise<WireResult> {
  const r = await apxDevFetch("/_apx/setup/apply-instructions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ instructions }),
  });
  const d = (await r.json().catch(() => ({}))) as WireResult;
  if (!r.ok || d.ok === false) {
    return {
      ok: false,
      detail: typeof d.detail === "string" ? d.detail : d.error || `HTTP ${r.status}`,
    };
  }
  return { ...d, ok: true };
}

export const WIRE_MIME = "application/x-apx-wire";
