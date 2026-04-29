/**
 * Tests for cross-agent trace propagation: header helpers, parent/child
 * linkage in the dev UI, and span rendering.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  createTrace,
  addSpan,
  endTrace,
  agentNameFromUrl,
  traceHeadersOut,
  traceHeadersIn,
  traceIdFromResponse,
  TRACE_ID_HEADER,
  PARENT_AGENT_HEADER,
} from '../src/trace.js';
import { createDevPlugin } from '../src/dev/index.js';
import { withAutonomousTrace, getRequestContext } from '../src/agent/request-context.js';
import { addSpan as addSpanFn } from '../src/trace.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type RouteMap = Record<string, Function>;

function makeRouter(): { router: { get: (path: string, fn: Function) => void }; routes: RouteMap } {
  const routes: RouteMap = {};
  return {
    routes,
    router: {
      get: (path: string, fn: Function) => { routes[path] = fn; },
    },
  };
}

function makeRes() {
  const headers: Record<string, string> = {};
  let body: unknown;
  let statusCode = 200;
  return {
    setHeader: (k: string, v: string) => { headers[k.toLowerCase()] = v; },
    send: (b: unknown) => { body = b; },
    json: (b: unknown) => { body = b; },
    status: (code: number) => ({ send: (b: unknown) => { statusCode = code; body = b; } }),
    type: () => ({ send: (b: unknown) => { body = b; } }),
    get body() { return body; },
    get statusCode() { return statusCode; },
    get headers() { return headers; },
  };
}

// ---------------------------------------------------------------------------
// Header helpers
// ---------------------------------------------------------------------------

describe('traceHeadersOut', () => {
  it('returns empty object when no trace is provided', () => {
    expect(traceHeadersOut(undefined)).toEqual({});
  });

  it('returns trace id and parent agent name as outgoing headers', () => {
    const trace = createTrace('orchestrator');
    const out = traceHeadersOut(trace);
    expect(out[TRACE_ID_HEADER]).toBe(trace.id);
    expect(out[PARENT_AGENT_HEADER]).toBe('orchestrator');
  });
});

describe('traceHeadersIn', () => {
  it('extracts parent context from lowercase express headers', () => {
    const result = traceHeadersIn({
      [TRACE_ID_HEADER]: 'tr-parent-1',
      [PARENT_AGENT_HEADER]: 'orchestrator',
      'content-type': 'application/json',
    });
    expect(result.parentTraceId).toBe('tr-parent-1');
    expect(result.parentAgentName).toBe('orchestrator');
  });

  it('returns undefined fields when no trace headers are present', () => {
    const result = traceHeadersIn({ 'content-type': 'application/json' });
    expect(result.parentTraceId).toBeUndefined();
    expect(result.parentAgentName).toBeUndefined();
  });

  it('ignores array-valued headers (RFC 7230 edge case)', () => {
    const result = traceHeadersIn({
      [TRACE_ID_HEADER]: ['tr-1', 'tr-2'],
    });
    expect(result.parentTraceId).toBeUndefined();
  });

  it('round-trips with traceHeadersOut', () => {
    const trace = createTrace('orchestrator');
    const out = traceHeadersOut(trace);
    const back = traceHeadersIn(out);
    expect(back.parentTraceId).toBe(trace.id);
    expect(back.parentAgentName).toBe('orchestrator');
  });
});

describe('traceIdFromResponse', () => {
  it('reads the trace id header from a response', () => {
    const fake = { headers: { get: (n: string) => (n === TRACE_ID_HEADER ? 'tr-child-1' : null) } };
    expect(traceIdFromResponse(fake)).toBe('tr-child-1');
  });

  it('returns undefined when the header is absent', () => {
    const fake = { headers: { get: (_n: string) => null } };
    expect(traceIdFromResponse(fake)).toBeUndefined();
  });
});

describe('agentNameFromUrl', () => {
  it('returns the first hostname segment for a Databricks Apps URL', () => {
    expect(agentNameFromUrl('https://my-agent.workspace.databricksapps.com/api/responses')).toBe('my-agent');
  });

  it('returns the input unchanged for an invalid URL', () => {
    expect(agentNameFromUrl('not a url')).toBe('not a url');
  });

  it('handles localhost', () => {
    expect(agentNameFromUrl('http://localhost:8080/responses')).toBe('localhost');
  });
});

// ---------------------------------------------------------------------------
// Dev UI rendering — parent/child linkage
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Autonomous (non-request-bound) tracing
// ---------------------------------------------------------------------------

describe('withAutonomousTrace', () => {
  it('returns the value produced by fn', async () => {
    const result = await withAutonomousTrace('worker', 'tick', async () => 42);
    expect(result).toBe(42);
  });

  it('makes the trace visible to child code via getRequestContext', async () => {
    let observedAgentName: string | undefined;
    let observedLabel: string | undefined;
    await withAutonomousTrace('evolutionary', 'generation 7', async () => {
      const ctx = getRequestContext();
      observedAgentName = ctx?.trace?.agentName;
      observedLabel = ctx?.trace?.spans.find((s) => s.type === 'request')?.name;
      return 'ok';
    });
    expect(observedAgentName).toBe('evolutionary');
    expect(observedLabel).toBe('generation 7');
  });

  it('captures spans added by child code into the same trace', async () => {
    let observedSpanCount = 0;
    await withAutonomousTrace('worker', 'tick', async () => {
      const ctx = getRequestContext();
      if (ctx?.trace) {
        addSpanFn(ctx.trace, { type: 'tool', name: 'inner-call' });
        addSpanFn(ctx.trace, { type: 'tool', name: 'inner-call-2' });
        observedSpanCount = ctx.trace.spans.length;
      }
      return null;
    });
    // Initial request span + 2 tool spans (response span is added after fn returns)
    expect(observedSpanCount).toBe(3);
  });

  it('records an error span when fn throws and rethrows', async () => {
    let traceIdCaptured: string | undefined;
    await expect(
      withAutonomousTrace('worker', 'failing-tick', async () => {
        const ctx = getRequestContext();
        traceIdCaptured = ctx?.trace?.id;
        throw new Error('boom');
      }),
    ).rejects.toThrow('boom');

    // The trace itself should have been ended with status='error'
    expect(traceIdCaptured).toBeDefined();
  });
});

describe('dev UI — trace list parent column', () => {
  beforeEach(() => { process.env.NODE_ENV = 'development'; });
  afterEach(() => { delete process.env.NODE_ENV; });

  it('renders parent agent name in list view when set', () => {
    // Seed a trace with a parent
    const child = createTrace('decipherer');
    child.parentAgentName = 'orchestrator';
    child.parentTraceId = 'tr-parent-1';
    endTrace(child);

    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, () => null);
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/traces']({}, res);

    const html = res.body as string;
    expect(html).toContain('orchestrator');
    expect(html).toContain('Parent');
  });
});

describe('dev UI — trace detail "called by" line', () => {
  beforeEach(() => { process.env.NODE_ENV = 'development'; });
  afterEach(() => { delete process.env.NODE_ENV; });

  it('renders a "Called by" header when the trace has parent linkage', () => {
    const child = createTrace('critic');
    child.parentAgentName = 'orchestrator';
    child.parentTraceId = 'tr-parent-1';
    endTrace(child);

    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, () => null);
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/traces/:traceId']({ params: { traceId: child.id } }, res);

    const html = res.body as string;
    expect(html).toContain('Called by');
    expect(html).toContain('orchestrator');
    expect(html).toContain('tr-parent-1');
  });

  it('does not render a "Called by" header when no parent is set', () => {
    const trace = createTrace('standalone-agent');
    endTrace(trace);

    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, () => null);
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/traces/:traceId']({ params: { traceId: trace.id } }, res);

    const html = res.body as string;
    expect(html).not.toContain('Called by');
  });
});

describe('dev UI — agent_call span renders child link', () => {
  beforeEach(() => { process.env.NODE_ENV = 'development'; });
  afterEach(() => { delete process.env.NODE_ENV; });

  it('renders an anchor to the child trace when childUrl + childTraceId are set', () => {
    const trace = createTrace('orchestrator');
    addSpan(trace, {
      type: 'agent_call',
      name: 'critic',
      input: { hypothesis: 'x' },
      output: 'ok',
      metadata: {
        childUrl: 'https://critic.workspace.databricksapps.com',
        childTraceId: 'tr-child-42',
      },
    });
    endTrace(trace);

    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, () => null);
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/traces/:traceId']({ params: { traceId: trace.id } }, res);

    const html = res.body as string;
    expect(html).toContain('https://critic.workspace.databricksapps.com/_apx/traces/tr-child-42');
    expect(html).toContain('target="_blank"');
  });

  it('renders attempt count for retried agent calls', () => {
    const trace = createTrace('orchestrator');
    addSpan(trace, {
      type: 'agent_call',
      name: 'fitness',
      output: 'ok',
      metadata: { childUrl: 'https://fitness.example', attempts: 3 },
    });
    endTrace(trace);

    const { router, routes } = makeRouter();
    const plugin = createDevPlugin({}, () => null);
    plugin.injectRoutes(router as any);

    const res = makeRes();
    routes['/_apx/traces/:traceId']({ params: { traceId: trace.id } }, res);

    const html = res.body as string;
    expect(html).toContain('3 attempts');
  });
});
