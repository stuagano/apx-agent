// Dev-UI write helpers (operator token passthrough) — used by NodeInspector Save.

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

export async function apxDevFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers || {});
  const token = readToken();
  if (token) headers.set("X-APX-Dev-Token", token);
  // Forward the dev-UI token when one is stored; on Apps, protected Dev-UI
  // writes require APX_DEV_UI_TOKEN while ordinary writes accept Apps SSO alone.
  return fetch(url, { ...init, headers });
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
