/**
 * Mount the MLflow ChatAgent ``/invocations`` route onto an Express app or
 * Router. TypeScript port of ``_invocations.py``.
 *
 * This is the wire protocol that Mosaic AI agent serving, the AI Playground,
 * and the Review App expect. Mounting it turns any apx-agent app into a
 * first-class Mosaic AI Agent at the wire level — no detour through Model
 * Serving needed.
 *
 * Wire shape (request):
 *
 *   POST /invocations
 *   {
 *     "messages": [{"role": "user", "content": "..."}],
 *     "context":  {"conversation_id": "...", "user_id": "..."},
 *     "customInputs": {},   // also accepted as snake_case "custom_inputs"
 *     "stream": false       // OR customInputs.stream = true (matches spec)
 *   }
 *
 * Non-streaming response (200, application/json):
 *
 *   { "messages": [...], "finish_reason": null, "custom_outputs": {}, "usage": {} }
 *
 * Streaming response (200, application/x-ndjson):
 *
 *   {<ChatAgentChunk JSON>}\n
 *   {<ChatAgentChunk JSON>}\n
 *   ...
 *
 * NDJSON (one chunk per line, no SSE framing) matches the task spec.
 * Python uses SSE; this is the deliberate JS-side simplification.
 *
 * OBO auth bridge: when running behind the Databricks Apps proxy, the
 * inbound request carries ``X-Forwarded-Access-Token``. If present, it's
 * forwarded as ``customInputs.user_token`` (and ``DATABRICKS_HOST`` as
 * ``customInputs.workspace_host``) so the downstream ChatAgent can build a
 * per-request user-scoped WorkspaceClient. A caller-provided ``user_token``
 * always wins.
 */

import type { Express, Router, Request, Response } from 'express';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Minimum surface a ``ChatAgent`` needs to be mountable. Duck-typed so the
 * concrete chat-agent module isn't a load-bearing import here.
 */
export interface ChatAgentLike {
  predict(
    messages: ChatAgentMessage[],
    opts?: ChatAgentPredictOptions,
  ): Promise<ChatAgentResponse> | ChatAgentResponse;
  predictStream(
    messages: ChatAgentMessage[],
    opts?: ChatAgentPredictOptions,
  ):
    | AsyncIterable<ChatAgentChunk>
    | Iterable<ChatAgentChunk>;
}

/** Single chat message — matches MLflow ChatAgentMessage shape. */
export interface ChatAgentMessage {
  role: string;
  content?: string | null;
  id?: string;
  [key: string]: unknown;
}

/** Optional context payload (per ChatAgent spec). */
export interface ChatContext {
  conversation_id?: string;
  user_id?: string;
  [key: string]: unknown;
}

/** Options forwarded into ``predict`` / ``predictStream``. */
export interface ChatAgentPredictOptions {
  context?: ChatContext;
  customInputs?: Record<string, unknown>;
}

/** Final non-streaming response shape. */
export interface ChatAgentResponse {
  messages: ChatAgentMessage[];
  finish_reason?: string | null;
  custom_outputs?: Record<string, unknown>;
  usage?: Record<string, unknown>;
  [key: string]: unknown;
}

/** Streaming chunk shape. */
export interface ChatAgentChunk {
  delta?: ChatAgentMessage;
  finish_reason?: string | null;
  custom_outputs?: Record<string, unknown>;
  [key: string]: unknown;
}

/** Options for ``mountInvocationsRoute``. */
export interface MountInvocationsOptions {
  /** Express app or Router on which to register the POST handler. */
  app: Express | Router;
  /** The ChatAgent (duck-typed). */
  chatAgent: ChatAgentLike;
  /** Path to mount. Default ``"/invocations"``. */
  path?: string;
  /**
   * API prefix used for the mirrored mount. The route is registered at
   * both ``path`` and ``${apiPrefix}${path}`` to match the Python
   * "natural path + /api/ mirror" convention. Default ``"/api"``.
   */
  apiPrefix?: string;
}

// ---------------------------------------------------------------------------
// Route handler
// ---------------------------------------------------------------------------

/**
 * Mount POST ``/invocations`` (and ``/api/invocations``) onto ``app``.
 *
 * Returns ``true`` on success, ``false`` if the supplied ``chatAgent``
 * lacks the required surface (no ``predict``). Mounting is best-effort;
 * a missing dependency must never crash the wider app.
 */
export function mountInvocationsRoute(opts: MountInvocationsOptions): boolean {
  const path = opts.path ?? '/invocations';
  const apiPrefix = opts.apiPrefix ?? '/api';
  const chatAgent = opts.chatAgent;

  if (!chatAgent || typeof chatAgent.predict !== 'function') {
    console.warn(
      '[invocations] chatAgent missing required predict() method — ' +
        'skipping mount. Provide a ChatAgentLike with predict + predictStream.',
    );
    return false;
  }
  const hasStream = typeof chatAgent.predictStream === 'function';

  const handler = async (req: Request, res: Response): Promise<void> => {
    let body: Record<string, unknown>;
    try {
      body = (req.body ?? {}) as Record<string, unknown>;
    } catch {
      res.status(400).json({ error: 'Invalid JSON body' });
      return;
    }

    const messagesRaw = body['messages'];
    if (!Array.isArray(messagesRaw)) {
      res.status(400).json({
        error:
          'Request body must include a "messages" array of ChatAgentMessage objects.',
      });
      return;
    }

    // Light shape validation — every message must have a role.
    const messages: ChatAgentMessage[] = [];
    for (let i = 0; i < messagesRaw.length; i++) {
      const m = messagesRaw[i] as Record<string, unknown> | null;
      if (m === null || typeof m !== 'object' || typeof m['role'] !== 'string') {
        res.status(400).json({
          error: `Invalid ChatAgentMessage at index ${i}: missing or non-string "role".`,
        });
        return;
      }
      messages.push(m as ChatAgentMessage);
    }

    // Accept both customInputs (camelCase, spec) and custom_inputs (snake_case, Python parity).
    const rawCustomInputs =
      (body['customInputs'] as Record<string, unknown> | undefined) ??
      (body['custom_inputs'] as Record<string, unknown> | undefined) ??
      {};
    const customInputs: Record<string, unknown> = { ...rawCustomInputs };
    const context = (body['context'] as ChatContext | undefined) ?? undefined;

    // --- OBO header bridge ---------------------------------------------
    // Caller-provided user_token wins over the proxy header.
    const forwardedTokenRaw =
      req.headers['x-forwarded-access-token'] ?? req.headers['X-Forwarded-Access-Token'];
    const forwardedToken = Array.isArray(forwardedTokenRaw)
      ? forwardedTokenRaw[0]
      : forwardedTokenRaw;
    if (typeof forwardedToken === 'string' && forwardedToken.length > 0 && !('user_token' in customInputs)) {
      customInputs['user_token'] = forwardedToken;
      const host = process.env.DATABRICKS_HOST;
      if (host && !('workspace_host' in customInputs)) {
        customInputs['workspace_host'] = host;
      }
    }

    // Streaming flag: prefer customInputs.stream (per task spec), fall back
    // to top-level "stream" for Python-protocol parity.
    const streamFlag =
      customInputs['stream'] === true ||
      body['stream'] === true;
    // Don't leak the meta-flag into the agent's customInputs.
    if ('stream' in customInputs) delete customInputs['stream'];

    const predictOpts: ChatAgentPredictOptions = {};
    if (context !== undefined) predictOpts.context = context;
    if (Object.keys(customInputs).length > 0) predictOpts.customInputs = customInputs;

    if (streamFlag && hasStream) {
      res.status(200);
      res.setHeader('Content-Type', 'application/x-ndjson');
      res.setHeader('Cache-Control', 'no-cache');
      try {
        const iter = chatAgent.predictStream(messages, predictOpts);
        for await (const chunk of toAsyncIterable(iter)) {
          res.write(`${JSON.stringify(chunk)}\n`);
        }
        res.end();
      } catch (err) {
        console.error('[invocations] stream error:', err);
        // Headers already sent — emit an error line and close.
        try {
          res.write(
            `${JSON.stringify({ error: 'Internal error during stream.' })}\n`,
          );
        } catch {
          /* connection may be closed */
        }
        res.end();
      }
      return;
    }

    // Non-streaming
    try {
      const response = await chatAgent.predict(messages, predictOpts);
      res.status(200).json(response);
    } catch (err) {
      // Sanitize: don't leak stack traces or internal error class names.
      console.error('[invocations] predict error:', err);
      res
        .status(500)
        .json({ error: 'Internal error processing invocation.' });
    }
  };

  const mirror = `${apiPrefix}${path}`;
  // Express types: both Express and Router expose .post(path, handler).
  (opts.app as { post: (p: string, h: typeof handler) => void }).post(path, handler);
  if (mirror !== path) {
    (opts.app as { post: (p: string, h: typeof handler) => void }).post(mirror, handler);
  }
  return true;
}

// ---------------------------------------------------------------------------
// Internals
// ---------------------------------------------------------------------------

/**
 * Convert a sync or async iterable into an AsyncIterable so the streaming
 * loop can ``for await`` over both shapes uniformly. ChatAgent JS bindings
 * may yield either form.
 */
async function* toAsyncIterable<T>(
  iter: AsyncIterable<T> | Iterable<T>,
): AsyncIterable<T> {
  if ((iter as AsyncIterable<T>)[Symbol.asyncIterator]) {
    for await (const item of iter as AsyncIterable<T>) {
      yield item;
    }
    return;
  }
  for (const item of iter as Iterable<T>) {
    yield item;
  }
}
