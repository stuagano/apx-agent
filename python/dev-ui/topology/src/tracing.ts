// Topology tracing helpers — experiment badge + last-turn route highlight.

export interface TracingInfo {
  experiment_id: string | null;
  experiment_name: string | null;
  workspace_host: string | null;
  experiment_url: string | null;
  configured: boolean;
}

export interface LastRoute {
  trace_id: string | null;
  node_ids: string[];
  edge_ids: string[];
  tool_names: string[];
  span_count: number;
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
  return fetch(url, { ...init, headers });
}

export async function fetchTracingInfo(): Promise<TracingInfo | null> {
  const r = await fetch("/_apx/topology/tracing");
  if (!r.ok) return null;
  return (await r.json()) as TracingInfo;
}

export async function setExperimentId(
  experimentId: string,
): Promise<{ ok: boolean; detail?: string; info?: TracingInfo }> {
  const r = await apxDevFetch("/_apx/topology/tracing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ experiment_id: experimentId }),
  });
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>;
  if (!r.ok || d.ok === false) {
    return {
      ok: false,
      detail:
        (typeof d.detail === "string" && d.detail) ||
        (typeof d.error === "string" && d.error) ||
        `HTTP ${r.status}`,
    };
  }
  return {
    ok: true,
    info: {
      experiment_id: (d.experiment_id as string) || experimentId,
      experiment_name: (d.experiment_name as string) || null,
      workspace_host: null,
      experiment_url: (d.experiment_url as string) || null,
      configured: true,
    },
  };
}

export async function fetchLastRoute(): Promise<LastRoute | null> {
  const r = await fetch("/_apx/traces/last-route");
  if (!r.ok) return null;
  return (await r.json()) as LastRoute;
}
