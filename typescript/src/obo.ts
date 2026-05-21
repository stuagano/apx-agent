/**
 * Unified OBO (on-behalf-of) header extraction — TS port of
 * `python/src/apx_agent/_obo.py`.
 *
 * The apx-agent SDK supports two Databricks runtime targets:
 *
 *   * **Model Serving** (`compileToChatAgent`): the OBO token is plumbed
 *     through `customInputs={user_token, workspace_host}`. An Express front
 *     door (`mountInvocationsRoute`) extracts the `X-Forwarded-Access-Token`
 *     header and forwards it as a `customInputs` field so the deployed
 *     pyfunc has access through the request payload.
 *
 *   * **Databricks Apps** (`compileToResponsesAgent`): the Apps proxy
 *     auto-injects `X-Forwarded-Access-Token`, `X-Forwarded-User`, and
 *     `X-Forwarded-Email` per request. The agent reads them directly from
 *     the incoming HTTP headers in the Node.js process serving the App.
 *
 * Both runtimes must end up at the same place — a per-request user-scoped
 * client whose downstream calls see the calling user's grants. The single
 * source of truth is {@link extractOboHeaders}, which normalizes BOTH input
 * shapes into a uniform shape:
 *
 *   { userToken, workspaceHost, userId, userEmail }
 *
 * Any field may be missing. Downstream consumers can plug `userToken` and
 * `workspaceHost` straight into the auth headers for fetch().
 *
 * Precedence rule (matters when both conventions are present in one request):
 *   1. `customInputs.user_token` — caller-supplied wins. This preserves the
 *      existing semantics from `invocations.ts` and the eval bridge, where a
 *      batch caller may want to override the route-injected token.
 *   2. `X-Forwarded-Access-Token` HTTP header — Apps runtime fallback.
 *
 * The same precedence applies to `workspaceHost`
 * (`customInputs.workspace_host` beats `X-Forwarded-Host` beats
 * `DATABRICKS_HOST` env), `userId` (`customInputs.user_id` beats
 * `X-Forwarded-User`), and `userEmail`.
 */

/** Normalized OBO context — any field may be absent. */
export interface NormalizedObo {
  /** OBO access token, suitable for `Authorization: Bearer ...`. */
  userToken?: string;
  /** Databricks workspace host (no scheme). */
  workspaceHost?: string;
  /** Databricks user id (SCIM ID, not email). */
  userId?: string;
  /** Databricks user email. */
  userEmail?: string;
}

/**
 * Anything header-shaped: the standard `Headers` object, a plain dict, or
 * Node's `IncomingHttpHeaders`. We accept all of them and normalize on read.
 */
export type HeadersInput =
  | Headers
  | Record<string, string | string[] | undefined>
  | undefined
  | null;

export interface ExtractOboHeadersOptions {
  /** `customInputs` carried on a ChatAgentRequest / ResponsesAgentRequest. */
  customInputs?: Record<string, unknown> | null | undefined;
  /** Request headers — `Headers`, plain object, or `IncomingHttpHeaders`. */
  headers?: HeadersInput;
}

/**
 * Case-insensitive header lookup that tolerates hyphen / underscore variants.
 *
 * Web-standard `Headers` already does case-insensitive lookup; plain objects
 * (e.g. `req.headers` from Express) lower-case keys but we still scan
 * defensively in case a caller passes a non-normalized dict.
 */
function headerLookup(headers: HeadersInput, name: string): string | undefined {
  if (!headers) return undefined;

  const lower = name.toLowerCase();
  const underscore = lower.replaceAll('-', '_');
  const candidates = new Set([lower, underscore, underscore.replaceAll('_', '-')]);

  // Web-standard Headers
  if (typeof Headers !== 'undefined' && headers instanceof Headers) {
    for (const candidate of candidates) {
      const value = headers.get(candidate);
      if (value) return value;
    }
    return undefined;
  }

  // Plain object (or IncomingHttpHeaders)
  const dict = headers as Record<string, string | string[] | undefined>;

  // Fast path — direct lookup
  const direct = dict[name] ?? dict[lower];
  if (direct !== undefined) {
    const v = Array.isArray(direct) ? direct[0] : direct;
    if (v) return v;
  }

  // Slow path — scan
  for (const key of Object.keys(dict)) {
    if (candidates.has(key.toLowerCase())) {
      const raw = dict[key];
      const v = Array.isArray(raw) ? raw[0] : raw;
      if (v) return v;
    }
  }
  return undefined;
}

function ciString(value: unknown): string | undefined {
  if (typeof value === 'string' && value.length > 0) return value;
  return undefined;
}

/**
 * Normalize OBO context from either runtime convention.
 *
 * @param opts
 * @returns A {@link NormalizedObo} with only the populated fields. Pass the
 *   result to your fetch() layer:
 *
 *   const obo = extractOboHeaders({ customInputs, headers: req.headers });
 *   if (obo.userToken) headers.Authorization = `Bearer ${obo.userToken}`;
 *
 * Precedence: `customInputs` wins over headers. `workspaceHost` additionally
 * falls back to the `DATABRICKS_HOST` env var when neither source provides
 * it. Hyphen / underscore / case variations in header names are tolerated.
 */
export function extractOboHeaders(
  opts: ExtractOboHeadersOptions = {},
): NormalizedObo {
  const ci = opts.customInputs ?? {};
  const headers = opts.headers;
  const out: NormalizedObo = {};

  // --- userToken (load-bearing) ----------------------------------------
  const ciToken = ciString(ci['user_token']);
  if (ciToken) {
    out.userToken = ciToken;
  } else {
    const headerToken = headerLookup(headers, 'X-Forwarded-Access-Token');
    if (headerToken) out.userToken = headerToken;
  }

  // --- workspaceHost ---------------------------------------------------
  const ciHost = ciString(ci['workspace_host']);
  if (ciHost) {
    out.workspaceHost = ciHost;
  } else {
    const headerHost = headerLookup(headers, 'X-Forwarded-Host');
    if (headerHost) {
      out.workspaceHost = headerHost;
    } else {
      const envHost = process.env.DATABRICKS_HOST;
      if (envHost && envHost.length > 0) out.workspaceHost = envHost;
    }
  }

  // --- userId ----------------------------------------------------------
  const ciUserId = ciString(ci['user_id']);
  if (ciUserId) {
    out.userId = ciUserId;
  } else {
    const headerUser = headerLookup(headers, 'X-Forwarded-User');
    if (headerUser) out.userId = headerUser;
  }

  // --- userEmail -------------------------------------------------------
  const ciEmail = ciString(ci['user_email']);
  if (ciEmail) {
    out.userEmail = ciEmail;
  } else {
    const headerEmail = headerLookup(headers, 'X-Forwarded-Email');
    if (headerEmail) out.userEmail = headerEmail;
  }

  return out;
}
