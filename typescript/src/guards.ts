/**
 * Local lightweight guards — zero-latency runtime checks.
 *
 * apx-agent's hook surface (`inputGuardrails`, `beforeTool`, `beforeModel`)
 * wants pre-built callables for the most common runtime checks: rate limit
 * a tool by user, reject obvious prompt injection, gate a tool to an
 * allowlist. These guards are deliberately *not* a competing policy engine —
 * they live in-process with zero network/database overhead.
 *
 * Each guard throws (for `before*` callbacks) or returns a reject string
 * (for `*Guardrails` callbacks). Exceptions propagate so the runtime can
 * surface them as 4xx-equivalent rejections.
 *
 * @example
 * ```ts
 * const agent = new Agent({
 *   inputGuardrails: [promptInjectionHeuristic()],
 *   beforeTool: compose(
 *     new RateLimit({ perMinute: 60 }),
 *     new ToolAllowlist(['classify_intent', 'get_recent_orders']),
 *   ),
 * });
 * ```
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// PermissionError
// ---------------------------------------------------------------------------

/**
 * Error class for guard rejections. Mirrors Python's `PermissionError` —
 * the runtime can detect this type and translate to a 403/4xx response.
 */
export class PermissionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PermissionError';
    // Maintain proper prototype chain for `instanceof` after transpile
    Object.setPrototypeOf(this, PermissionError.prototype);
  }
}

// ---------------------------------------------------------------------------
// RateLimit — token bucket per principal
// ---------------------------------------------------------------------------

const RateLimitOptionsSchema = z.object({
  perMinute: z.number().int().positive(),
  burst: z.number().int().positive().optional(),
  message: z.string().optional(),
});

export interface RateLimitOptions {
  /** Sustained rate in calls per minute (must be > 0). */
  perMinute: number;
  /** Max burst capacity (defaults to `perMinute`). */
  burst?: number;
  /** Extract a principal key from `(name, args)` for per-principal buckets. */
  principalKey?: (name: string, args: Record<string, unknown>) => unknown;
  /** Custom rejection message. */
  message?: string;
}

/**
 * Token-bucket rate limit for the `beforeTool` (or `beforeModel`) hook.
 *
 * Refills at `perMinute / 60` tokens per second up to `burst` tokens.
 * Per-principal — the key extractor pulls a principal from `(name, args)`
 * (defaults to a single global bucket). When the bucket runs dry, throws
 * `PermissionError`.
 *
 * No locking needed: JS runs on a single event loop, so the read-modify-write
 * of bucket state is atomic by construction.
 *
 * @example
 * ```ts
 * // 60 calls per minute agent-wide; burst 10
 * beforeTool: new RateLimit({ perMinute: 60, burst: 10 })
 *
 * // 30 calls per minute scoped to a user_id argument
 * beforeTool: new RateLimit({
 *   perMinute: 30,
 *   principalKey: (_name, args) => args.user_id,
 * })
 * ```
 */
export class RateLimit {
  readonly perMinute: number;
  readonly burst: number;
  readonly principalKey?: (name: string, args: Record<string, unknown>) => unknown;
  readonly message: string;

  private readonly tokens = new Map<unknown, number>();
  private readonly lastRefill = new Map<unknown, number>();

  constructor(options: RateLimitOptions) {
    const parsed = RateLimitOptionsSchema.parse({
      perMinute: options.perMinute,
      burst: options.burst,
      message: options.message,
    });
    this.perMinute = parsed.perMinute;
    this.burst = parsed.burst ?? parsed.perMinute;
    this.principalKey = options.principalKey;
    this.message = parsed.message ?? `Rate limit exceeded: ${this.perMinute}/min.`;
  }

  private now(): number {
    // Wall-clock milliseconds — controllable by vi.setSystemTime in tests.
    // We could use performance.now() for true monotonicity, but Date.now()
    // is sufficient for rate limiting and stays testable. A backwards wall-clock
    // jump just delays the next refill, never grants extra tokens (we clamp
    // elapsed time below by treating negative deltas as zero).
    return Date.now();
  }

  private takeToken(principal: unknown): boolean {
    const nowMs = this.now();
    const rate = this.perMinute / 60.0; // tokens per second
    const last = this.lastRefill.get(principal) ?? nowMs;
    const elapsedSec = Math.max(0, (nowMs - last) / 1000.0);
    const current = this.tokens.get(principal) ?? this.burst;
    const refilled = Math.min(this.burst, current + elapsedSec * rate);
    this.lastRefill.set(principal, nowMs);
    if (refilled >= 1.0) {
      this.tokens.set(principal, refilled - 1.0);
      return true;
    }
    this.tokens.set(principal, refilled);
    return false;
  }

  /** Check the rate limit; throws `PermissionError` when exhausted. */
  check(name: string, args: Record<string, unknown>): void {
    const principal = this.principalKey ? this.principalKey(name, args) : null;
    if (!this.takeToken(principal)) {
      throw new PermissionError(this.message);
    }
  }
}

// ---------------------------------------------------------------------------
// Prompt injection heuristic
// ---------------------------------------------------------------------------

/**
 * Common injection patterns. Intentionally small and high-specificity —
 * this is a fast-path heuristic, not a full classifier. False negatives are
 * expected; downstream (Watchdog, LLM-as-judge) catches what slips through.
 * False positives matter more, so each pattern requires some context.
 */
const DEFAULT_INJECTION_PATTERNS: ReadonlyArray<RegExp> = [
  /ignore (?:all (?:your )?)?(?:previous|prior|above) (?:instructions|prompts|rules)/i,
  /disregard (?:all (?:of )?)?(?:the )?(?:above|previous|prior)/i,
  /forget (?:everything|all) (?:before|above)/i,
  /you are now (?:a |an )?(?!helpful|kind|polite|professional)/i,
  /system\s*prompt\s*:/i,
  /<\|(?:im_start|im_end|system|user|assistant)\|>/i,
  /\[\[SYSTEM\]\]/i,
  /reveal (?:your |the )(?:system|hidden|secret) prompt/i,
  /print (?:your |the )(?:instructions|system prompt|initial prompt)/i,
];

export interface PromptInjectionOptions {
  /** Replace the default pattern set entirely. */
  patterns?: ReadonlyArray<RegExp>;
  /** Custom rejection message. */
  message?: string;
}

/** A message-like input accepted by `promptInjectionHeuristic`. */
export type MessageLike =
  | string
  | Array<{ role?: string; content?: unknown } | { content?: unknown }>;

/**
 * Return an `inputGuardrails`-compatible function that flags injection patterns.
 *
 * Scans each message's `content` for known injection-attempt patterns. Returns
 * `message` to short-circuit the request when a pattern matches; returns
 * `null` otherwise.
 *
 * Accepts a plain string, an array of `{ role, content }` objects, or any
 * array of objects with a `.content` property.
 *
 * @example
 * ```ts
 * const guard = promptInjectionHeuristic();
 * const reject = guard([{ role: 'user', content: input }]);
 * if (reject) return reject;
 * ```
 */
export function promptInjectionHeuristic(
  options: PromptInjectionOptions = {},
): (messages: MessageLike) => string | null {
  const activePatterns = options.patterns ? [...options.patterns] : [...DEFAULT_INJECTION_PATTERNS];
  const rejectMessage = options.message ?? 'Request blocked: suspected prompt injection.';

  return (messages: MessageLike): string | null => {
    const texts = textsFromMessages(messages);
    for (const text of texts) {
      for (const pat of activePatterns) {
        if (pat.test(text)) {
          return rejectMessage;
        }
      }
    }
    return null;
  };
}

/**
 * Pull text out of whatever message shape was passed.
 *
 * Accepts: a string, a list of dicts with `content`, a list of objects with
 * a `.content` property. Defensive — returns an empty list rather than
 * throwing on unfamiliar shapes.
 */
function textsFromMessages(messages: MessageLike): string[] {
  if (typeof messages === 'string') return [messages];
  if (!Array.isArray(messages)) return [];
  const out: string[] = [];
  for (const m of messages) {
    if (m == null) continue;
    const content = (m as { content?: unknown }).content;
    if (typeof content === 'string') out.push(content);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Tool allowlist / denylist
// ---------------------------------------------------------------------------

export interface ToolListOptions {
  /** Custom rejection message. */
  message?: string;
}

/**
 * Reject any tool call whose name isn't in the allowlist.
 *
 * Plugs into `beforeTool`. Cheaper than a remote policy round-trip for a
 * fixed, well-known set of allowed tools.
 */
export class ToolAllowlist {
  readonly allowed: Set<string>;
  readonly message?: string;

  constructor(names: Iterable<string>, options: ToolListOptions = {}) {
    this.allowed = new Set(names);
    this.message = options.message;
  }

  /** Throws `PermissionError` if `name` is not in the allowlist. */
  check(name: string, _args: Record<string, unknown>): void {
    if (!this.allowed.has(name)) {
      throw new PermissionError(this.message ?? `Tool '${name}' not in allowlist.`);
    }
  }
}

/** Reject any tool call whose name IS in the denylist. */
export class ToolDenylist {
  readonly denied: Set<string>;
  readonly message?: string;

  constructor(names: Iterable<string>, options: ToolListOptions = {}) {
    this.denied = new Set(names);
    this.message = options.message;
  }

  /** Throws `PermissionError` if `name` is in the denylist. */
  check(name: string, _args: Record<string, unknown>): void {
    if (this.denied.has(name)) {
      throw new PermissionError(this.message ?? `Tool '${name}' is denied.`);
    }
  }
}

// ---------------------------------------------------------------------------
// Composition
// ---------------------------------------------------------------------------

/**
 * A guard callback shape. Accepts either:
 *   - a plain function `(name, args) => void | string | null`
 *   - a class instance with a `.check(name, args)` method (e.g. RateLimit)
 *   - a function `(messages) => string | null` for input-guardrail-style hooks
 */
export type GuardCallback =
  | ((...args: unknown[]) => unknown)
  | { check: (...args: unknown[]) => unknown };

function invokeCallback(cb: GuardCallback, args: unknown[]): unknown {
  if (typeof cb === 'function') {
    return (cb as (...a: unknown[]) => unknown)(...args);
  }
  if (cb && typeof (cb as { check?: unknown }).check === 'function') {
    return (cb as { check: (...a: unknown[]) => unknown }).check(...args);
  }
  throw new TypeError('compose: callback is not a function or guard with .check()');
}

/**
 * Chain multiple guard callbacks into one.
 *
 * The returned callable invokes each callback in order with the same
 * arguments. If any callback throws, the chain stops and the exception
 * propagates — useful for short-circuiting on the first rejection.
 *
 * For `inputGuardrails`-style hooks: a non-null / non-empty return value
 * short-circuits with that value (e.g. a reject string).
 *
 * For `beforeTool` / `beforeModel`-style hooks: callbacks throw on
 * rejection and otherwise return `undefined`/`null`, so the chain runs all
 * callbacks in order.
 *
 * @example
 * ```ts
 * const beforeTool = compose(
 *   new RateLimit({ perMinute: 60 }),
 *   new ToolAllowlist(['classify_intent']),
 * );
 * beforeTool('classify_intent', {});
 * ```
 */
export function compose(...callbacks: GuardCallback[]): (...args: unknown[]) => unknown {
  return (...args: unknown[]): unknown => {
    let result: unknown = null;
    for (const cb of callbacks) {
      result = invokeCallback(cb, args);
      // input_guardrails convention: a non-null / non-empty return short-circuits.
      // before_tool / before_model callbacks return undefined/null, so this is
      // a no-op for them.
      if (result !== null && result !== undefined && result !== '') {
        return result;
      }
    }
    return result;
  };
}
