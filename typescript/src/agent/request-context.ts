/**
 * Per-request context propagated via AsyncLocalStorage.
 *
 * The runner sets this before calling each tool handler so that tools
 * (genieTool, connector tools, etc.) can transparently access OBO auth
 * headers without needing to receive them as explicit arguments.
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import type { Trace } from '../trace.js';
import { createTrace, addSpan, endTrace, truncate } from '../trace.js';

export interface RequestContext {
  /** OBO and auth headers forwarded from the incoming HTTP request. */
  oboHeaders: Record<string, string>;
  /** Optional distributed-tracing handle for the current request. */
  trace?: Trace;
}

const storage = new AsyncLocalStorage<RequestContext>();

/** Run `fn` with the given context available to all async descendants. */
export function runWithContext<T>(ctx: RequestContext, fn: () => T): T {
  return storage.run(ctx, fn);
}

/** Return the current request context, or undefined outside a request. */
export function getRequestContext(): RequestContext | undefined {
  return storage.getStore();
}

/**
 * Run a unit of autonomous (non-request-bound) work as its own trace.
 *
 * Background loops — evolutionary generations, scheduled jobs, polling
 * workers — have no incoming request, so by default no trace is in scope
 * and child code's spans are silently dropped. Wrap each unit of work
 * (one generation, one tick, one job execution) with this helper so it
 * gets its own row in `/_apx/traces` with all child spans nested under it.
 */
export async function withAutonomousTrace<T>(
  agentName: string,
  label: string,
  fn: () => Promise<T>,
): Promise<T> {
  const trace = createTrace(agentName);
  addSpan(trace, { type: 'request', name: label, input: label });
  try {
    const result = await runWithContext({ oboHeaders: {}, trace }, fn);
    addSpan(trace, { type: 'response', name: 'response', output: truncate(result) });
    endTrace(trace);
    return result;
  } catch (err) {
    addSpan(trace, {
      type: 'error',
      name: 'error',
      metadata: { error: (err as Error).message ?? String(err) },
    });
    endTrace(trace, 'error');
    throw err;
  }
}
