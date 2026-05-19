/**
 * MLflow tracing helpers — TypeScript port of ``_mlflow_tracing.py``.
 *
 * MLflow has no first-party JavaScript SDK, so this module is **pluggable**.
 * Default behavior is no-op: spans are inert and ``isMlflowAvailable()``
 * returns ``false``. Callers who wire their own MLflow-compatible tracer
 * (or any other span sink — OTEL, Logfire, console) inject:
 *
 *   - ``setMlflowStartSpan(fn)`` — factory that returns a SpanLike, or null.
 *   - ``setActiveSpanResolver(fn)`` — returns the currently active span.
 *   - ``setMlflowAvailabilityCheck(fn)`` — drives ``isMlflowAvailable()``.
 *
 * Design principles (mirrored from the Python original):
 *
 *   1. No-op when no tracer is registered. Compile / agent paths work
 *      without any tracing wired up — installing tracing is opt-in.
 *   2. Errors during tracing must NEVER break requests. Every helper
 *      catches and logs via ``console.warn`` / ``console.debug``.
 *   3. The traced block always runs to completion even if span emission
 *      fails partway through.
 *
 * Span-type conventions (advisory — your tracer is free to ignore them):
 *
 *   * Top-level ChatAgent predict / predictStream → ``"AGENT"``
 *   * ``/invocations`` route handler              → ``"CHAIN"``
 *   * Compiled graph invocation                   → ``"CHAIN"``
 *   * Individual tool dispatch                    → ``"TOOL"``
 *
 * The ``SpanLike`` shape is re-exported from ``./audit.js`` and extended
 * with the output-setting + lifecycle helpers MLflow-style tracers expose.
 */

import type { SpanLike as BaseSpanLike } from './audit.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Extended span shape used by tracing helpers. Inherits the minimal
 * ``setAttribute`` from audit.ts; layers on the output + lifecycle methods
 * that MLflow-style spans expose.
 *
 * Every method is optional. Tracers may implement only what they support;
 * helpers degrade gracefully when a method is missing.
 */
export interface SpanLike extends BaseSpanLike {
  /** Record output payload on the span. Must be JSON-serializable. */
  setOutputs?(outputs: unknown): void;
  /** Record input payload on the span. Must be JSON-serializable. */
  setInputs?(inputs: unknown): void;
  /** End the span, marking it complete. Idempotent. */
  end?(): void;
}

/** Options accepted by ``safeSpan`` / ``withSafeSpan``. */
export interface SafeSpanOptions {
  /**
   * One of MLflow's SpanType strings — ``"AGENT"``, ``"CHAIN"``, ``"TOOL"``,
   * ``"LLM"``, ``"RETRIEVER"``, etc. Defaults to ``"UNKNOWN"``.
   */
  spanType?: string;
  /** Input payload (JSON-serializable). Recorded via ``setInputs`` if supported. */
  inputs?: unknown;
  /** Extra attributes recorded via ``setAttribute``. */
  attributes?: Record<string, unknown>;
}

/** Factory hook — given a span name + options, return a span or null. */
export type MlflowStartSpan = (
  name: string,
  opts: SafeSpanOptions,
) => SpanLike | null;

/** Hook — return the currently active span (e.g. an OTEL context lookup). */
export type ActiveSpanResolver = () => SpanLike | null;

/** Hook — drives ``isMlflowAvailable()``. */
export type MlflowAvailabilityCheck = () => boolean;

// ---------------------------------------------------------------------------
// Pluggable state — default to no-op
// ---------------------------------------------------------------------------

let _startSpan: MlflowStartSpan | null = null;
let _activeSpanResolver: ActiveSpanResolver = () => null;
let _availabilityCheck: MlflowAvailabilityCheck = () => false;

/**
 * Register the span factory. Called by integrations that wire up MLflow,
 * OTEL, or any other tracer.
 *
 * Pass ``null`` to revert to no-op.
 */
export function setMlflowStartSpan(fn: MlflowStartSpan | null): void {
  _startSpan = fn;
}

/**
 * Register the active-span resolver. Returns the span currently in scope,
 * or ``null`` if no tracer is active.
 *
 * Pass ``null`` to revert to the default (always-null) resolver.
 */
export function setActiveSpanResolver(fn: ActiveSpanResolver | null): void {
  _activeSpanResolver = fn ?? (() => null);
}

/**
 * Register the availability check. ``isMlflowAvailable`` calls this and
 * returns the result.
 *
 * Pass ``null`` to revert to the default (always-false) check.
 */
export function setMlflowAvailabilityCheck(
  fn: MlflowAvailabilityCheck | null,
): void {
  _availabilityCheck = fn ?? (() => false);
}

// ---------------------------------------------------------------------------
// Public helpers
// ---------------------------------------------------------------------------

/**
 * Return ``true`` iff a tracing backend has been registered. Default
 * behavior returns ``false`` — TypeScript has no first-party MLflow SDK.
 */
export function isMlflowAvailable(): boolean {
  try {
    return _availabilityCheck();
  } catch {
    return false;
  }
}

/**
 * No-op in TypeScript — equivalent of Python's
 * ``mlflow.langchain.autolog()`` doesn't exist in the JS ecosystem yet.
 *
 * Returns ``false`` to signal autolog could not be enabled.
 */
export function enableLangchainAutolog(): boolean {
  console.warn(
    '[mlflow-tracing] enableLangchainAutolog: no LangChain JS autolog ' +
      'support — returning false. Wire your tracer via setMlflowStartSpan ' +
      'and emit spans manually instead.',
  );
  return false;
}

/**
 * If ``APX_AGENT_MLFLOW_AUTOLOG=1`` (or ``true``/``yes``), call
 * ``enableLangchainAutolog``. Cheap no-op when the env var is missing.
 */
export function autologIfEnv(): void {
  const flag = (process.env.APX_AGENT_MLFLOW_AUTOLOG ?? '').trim().toLowerCase();
  if (flag === '1' || flag === 'true' || flag === 'yes') {
    enableLangchainAutolog();
  }
}

// ---------------------------------------------------------------------------
// Span construction
// ---------------------------------------------------------------------------

/** No-op span used when no tracer is registered. Methods swallow all calls. */
const NOOP_SPAN: SpanLike = Object.freeze({
  setAttribute() {
    /* no-op */
  },
  setOutputs() {
    /* no-op */
  },
  setInputs() {
    /* no-op */
  },
  end() {
    /* no-op */
  },
});

/**
 * Create a span. **The caller is responsible for ending it** (call
 * ``span.end()`` in a ``finally`` block). For the context-manager
 * ergonomics of Python's ``with safe_span(...)``, see ``withSafeSpan``.
 *
 * Returns a span-like object that is always non-null:
 *
 *   - If a tracer is registered and ``startSpan`` returns a span, that span.
 *   - If the tracer raises or returns null, a no-op span (with a warning).
 *   - If no tracer is registered, a no-op span.
 *
 * Returning a non-null span (even a no-op) keeps call-site code simple:
 *
 * ```ts
 * const span = safeSpan('foo', { spanType: 'CHAIN' });
 * try {
 *   const result = doWork();
 *   setSpanOutputs(span, result);
 *   return result;
 * } finally {
 *   span.end?.();
 * }
 * ```
 */
export function safeSpan(name: string, opts: SafeSpanOptions = {}): SpanLike {
  const factory = _startSpan;
  if (factory === null) {
    return NOOP_SPAN;
  }
  let span: SpanLike | null;
  try {
    span = factory(name, opts);
  } catch (err) {
    console.warn(
      `[mlflow-tracing] safeSpan(${JSON.stringify(name)}): ` +
        `tracer threw, falling back to no-op: ${(err as Error).message}`,
    );
    return NOOP_SPAN;
  }
  if (span === null || span === undefined) {
    return NOOP_SPAN;
  }
  // Apply inputs + attributes defensively; never let setup blow up the span.
  try {
    if (opts.inputs !== undefined && typeof span.setInputs === 'function') {
      span.setInputs(opts.inputs);
    }
    if (opts.attributes) {
      for (const [k, v] of Object.entries(opts.attributes)) {
        if (v === undefined || v === null) continue;
        try {
          span.setAttribute(k, v);
        } catch {
          /* swallow — broken span must not break the request */
        }
      }
    }
  } catch (err) {
    console.debug(
      `[mlflow-tracing] safeSpan setup failed: ${(err as Error).message}`,
    );
  }
  return span;
}

/**
 * Context-manager-style wrapper. Mirrors Python's
 * ``with safe_span(...) as span: ...`` ergonomics.
 *
 * The span is created before ``fn`` runs and ended in a ``finally`` block,
 * even if ``fn`` throws. The callback's return value is passed through.
 *
 * ```ts
 * const result = await withSafeSpan('predict', async (span) => {
 *   const r = await doWork();
 *   setSpanOutputs(span, r);
 *   return r;
 * }, { spanType: 'AGENT' });
 * ```
 */
export async function withSafeSpan<T>(
  name: string,
  fn: (span: SpanLike) => T | Promise<T>,
  opts: SafeSpanOptions = {},
): Promise<T> {
  const span = safeSpan(name, opts);
  try {
    return await fn(span);
  } finally {
    try {
      span.end?.();
    } catch (err) {
      console.debug(
        `[mlflow-tracing] span.end failed: ${(err as Error).message}`,
      );
    }
  }
}

// ---------------------------------------------------------------------------
// Safe attribute / output setters
// ---------------------------------------------------------------------------

/**
 * Safely call ``span.setOutputs(outputs)``. No-op if ``span`` is null,
 * lacks the method, or throws.
 */
export function setSpanOutputs(
  span: SpanLike | null | undefined,
  outputs: unknown,
): void {
  if (span === null || span === undefined) return;
  if (typeof span.setOutputs !== 'function') return;
  try {
    span.setOutputs(outputs);
  } catch (err) {
    console.debug(
      `[mlflow-tracing] setSpanOutputs failed: ${(err as Error).message}`,
    );
  }
}

/**
 * Safely set a single attribute on a span. Mirrors the helper in
 * ``audit.ts`` but tolerates a missing ``setAttribute`` method and
 * accepts ``null`` / ``undefined`` spans.
 */
export function setSpanAttribute(
  span: SpanLike | null | undefined,
  key: string,
  value: unknown,
): void {
  if (span === null || span === undefined) return;
  if (typeof span.setAttribute !== 'function') return;
  if (value === null || value === undefined) return;
  try {
    span.setAttribute(key, value);
  } catch (err) {
    console.debug(
      `[mlflow-tracing] setSpanAttribute(${JSON.stringify(key)}) failed: ${
        (err as Error).message
      }`,
    );
  }
}

/**
 * Return the currently active span if a resolver is registered; otherwise
 * ``null``. Used by callbacks that attach attributes to an outer span
 * without owning its lifecycle.
 */
export function currentActiveSpan(): SpanLike | null {
  try {
    return _activeSpanResolver();
  } catch {
    return null;
  }
}
