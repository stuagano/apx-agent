/**
 * Per-request context propagated via AsyncLocalStorage.
 *
 * The runner sets this before calling each tool handler so that tools
 * (genieTool, connector tools, etc.) can transparently access OBO auth
 * headers without needing to receive them as explicit arguments.
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import type { Trace } from '../trace.js';

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
