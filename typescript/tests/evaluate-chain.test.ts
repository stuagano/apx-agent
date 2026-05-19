/**
 * Tests for evaluateChain() — TS port of python/tests/test_eval_chain.py.
 *
 * Covers:
 *   1. requestTextsFromEvalset tolerates list-of-dicts, list-of-strings,
 *      and DataFrame-like (`to_dict(orient='records')`) shapes.
 *   2. evaluateChain pairs traces to evalset rows by request-text
 *      containment on the root span's inputs.
 *   3. Sub-agent endpoints + tool names are pulled off child span
 *      `apx.subagent.endpoint` / `apx.subagent.name` / `apx.tool.name`
 *      attributes.
 *   4. Sub-agent coverage aggregates across cases.
 *   5. Cases without a matching trace are dropped (not crashed).
 *   6. `userToken` / `workspaceHost` / `experiment` are forwarded to the
 *      underlying `evaluate()` call.
 *   7. A `traceSearch` rejection yields an empty cases array (no throw).
 *   8. The returned report + each case is frozen.
 *
 * Both `mlflowEvaluate` and `traceSearch` are injected mocks so tests run
 * with no real workspace or mlflow dependency.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  evaluateChain,
  requestTextsFromEvalset,
  buildCaseFromTrace,
  type ChainEvalReport,
  type EvaluateChainOptions,
  type TraceSearchFn,
} from '../src/evaluate-chain.js';
import type {
  MlflowEvaluateFn,
  MlflowEvaluateArgs,
} from '../src/evaluate.js';
import type { RunViaSDKFn } from '../src/run-once.js';
import type { AgentConfig } from '../src/agent/plugin.js';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeConfig(overrides: Partial<AgentConfig> = {}): AgentConfig {
  return {
    name: 'supervisor',
    model: 'databricks-claude-sonnet-4-6',
    instructions: 'You route to sub-agents.',
    tools: [],
    ...overrides,
  };
}

function makeRunner(text = 'supervisor reply'): RunViaSDKFn {
  return vi.fn(async () => text) as unknown as RunViaSDKFn;
}

function makeMlflowEvaluate(returnValue: unknown = { metrics: {} }): {
  fn: MlflowEvaluateFn;
  lastArgs: () => MlflowEvaluateArgs;
  calls: () => unknown[];
} {
  const mock = vi.fn(async () => returnValue);
  return {
    fn: mock as unknown as MlflowEvaluateFn,
    lastArgs: () =>
      (mock as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as MlflowEvaluateArgs,
    calls: () => (mock as unknown as ReturnType<typeof vi.fn>).mock.calls,
  };
}

interface FakeTraceOpts {
  requestText: string;
  subAgentEndpoints?: string[];
  subAgentNames?: string[];
  toolNames?: string[];
  durationMs?: number;
  traceId?: string;
  response?: string;
}

/** Build a fake Trace-object whose root span carries the request text. */
function fakeTrace(opts: FakeTraceOpts): unknown {
  const {
    requestText,
    subAgentEndpoints = [],
    subAgentNames = [],
    toolNames = [],
    durationMs = 100,
    traceId = 'tr-1',
    response = 'supervisor reply',
  } = opts;

  const rootSpan = {
    attributes: { 'apx.operation': 'predict' },
    inputs: { messages: [{ role: 'user', content: requestText }] },
    outputs: { messages: [{ role: 'assistant', content: response }] },
  };
  const childSpans: Array<{
    attributes: Record<string, unknown>;
    inputs: unknown;
    outputs: unknown;
  }> = [];
  for (const endpoint of subAgentEndpoints) {
    childSpans.push({
      attributes: { 'apx.subagent.endpoint': endpoint },
      inputs: null,
      outputs: null,
    });
  }
  for (const name of subAgentNames) {
    childSpans.push({
      attributes: { 'apx.subagent.name': name },
      inputs: null,
      outputs: null,
    });
  }
  for (const toolName of toolNames) {
    childSpans.push({
      attributes: { 'apx.tool.name': toolName },
      inputs: null,
      outputs: null,
    });
  }

  return {
    info: {
      trace_id: traceId,
      execution_time_ms: durationMs,
      tags: {},
    },
    data: { spans: [rootSpan, ...childSpans] },
  };
}

function makeTraceSearch(traces: unknown[]): TraceSearchFn {
  return vi.fn(async () => traces) as unknown as TraceSearchFn;
}

function baseOpts(overrides: Partial<EvaluateChainOptions> = {}): EvaluateChainOptions {
  return {
    agent: makeConfig(),
    model: 'databricks-claude-sonnet-4-6',
    evalset: [],
    experiment: '/Users/me/agents/triage',
    mlflowEvaluate: makeMlflowEvaluate().fn,
    runViaSDK: makeRunner(),
    traceSearch: makeTraceSearch([]),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// requestTextsFromEvalset
// ---------------------------------------------------------------------------

describe('requestTextsFromEvalset', () => {
  it('reads request texts from a list of dicts', () => {
    const rows = [{ request: 'q1' }, { input: 'q2' }, { prompt: 'q3' }];
    expect(requestTextsFromEvalset(rows)).toEqual(['q1', 'q2', 'q3']);
  });

  it('reads request texts from a list of strings', () => {
    expect(requestTextsFromEvalset(['q1', 'q2'])).toEqual(['q1', 'q2']);
  });

  it('reads request texts from a DataFrame-like (to_dict)', () => {
    const df = {
      to_dict: (_orient: string) => [{ query: 'q1' }, { question: 'q2' }],
    };
    expect(requestTextsFromEvalset(df)).toEqual(['q1', 'q2']);
  });

  it('returns [] for unsupported shapes', () => {
    expect(requestTextsFromEvalset(null)).toEqual([]);
    expect(requestTextsFromEvalset(undefined)).toEqual([]);
    expect(requestTextsFromEvalset(42)).toEqual([]);
  });

  it('skips rows with empty / missing prompt keys', () => {
    const rows = [{ request: '' }, { input: null }, { prompt: 'kept' }];
    expect(requestTextsFromEvalset(rows)).toEqual(['kept']);
  });

  it('coerces non-string prompt-key values via String()', () => {
    expect(requestTextsFromEvalset([{ request: 123 }])).toEqual(['123']);
  });
});

// ---------------------------------------------------------------------------
// buildCaseFromTrace
// ---------------------------------------------------------------------------

describe('buildCaseFromTrace', () => {
  it('returns null when no trace matches', () => {
    const result = buildCaseFromTrace('missing', [
      fakeTrace({ requestText: 'other' }),
    ]);
    expect(result).toBeNull();
  });

  it('returns null when traces is null / undefined', () => {
    expect(buildCaseFromTrace('q', null)).toBeNull();
    expect(buildCaseFromTrace('q', undefined)).toBeNull();
    expect(buildCaseFromTrace('q', [])).toBeNull();
  });

  it('extracts sub-agent endpoint + tool name attrs', () => {
    const trace = fakeTrace({
      requestText: 'q1',
      subAgentEndpoints: ['billing'],
      toolNames: ['classify_intent', 'get_lineage'],
      traceId: 't-1',
      durationMs: 200,
    });
    const c = buildCaseFromTrace('q1', [trace]);
    expect(c).not.toBeNull();
    expect(c!.subAgentsInvoked).toEqual(['billing']);
    expect(c!.toolCalls).toEqual(['classify_intent', 'get_lineage']);
    expect(c!.traceId).toBe('t-1');
    expect(c!.durationMs).toBe(200);
    expect(c!.response).toBe('supervisor reply');
  });

  it('falls back to apx.subagent.name when endpoint is absent', () => {
    const trace = fakeTrace({
      requestText: 'q1',
      subAgentNames: ['billing-specialist'],
    });
    const c = buildCaseFromTrace('q1', [trace]);
    expect(c!.subAgentsInvoked).toEqual(['billing-specialist']);
  });

  it('returns a frozen case', () => {
    const trace = fakeTrace({ requestText: 'q1' });
    const c = buildCaseFromTrace('q1', [trace]);
    expect(Object.isFrozen(c)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// evaluateChain — happy path: pair traces to evalset by request text
// ---------------------------------------------------------------------------

describe('evaluateChain — pairing', () => {
  it('pairs traces to evalset rows by request-text containment', async () => {
    const traces = [
      fakeTrace({
        requestText: 'what is the lineage of orders?',
        subAgentEndpoints: ['data-triage'],
        toolNames: ['classify_intent', 'get_lineage'],
        traceId: 'tr-1',
      }),
      fakeTrace({
        requestText: 'refund my last invoice',
        subAgentEndpoints: ['billing-specialist'],
        toolNames: ['classify_intent', 'get_recent_orders'],
        traceId: 'tr-2',
      }),
    ];

    const evalset = [
      { request: 'what is the lineage of orders?' },
      { request: 'refund my last invoice' },
    ];

    const ml = makeMlflowEvaluate();
    const ts = makeTraceSearch(traces);

    const report: ChainEvalReport = await evaluateChain(
      baseOpts({ evalset, mlflowEvaluate: ml.fn, traceSearch: ts }),
    );

    expect(report.cases).toHaveLength(2);
    const lineageCase = report.cases.find((c) => c.request.includes('lineage'))!;
    const refundCase = report.cases.find((c) => c.request.includes('refund'))!;
    expect(lineageCase.subAgentsInvoked).toEqual(['data-triage']);
    expect(lineageCase.toolCalls).toContain('get_lineage');
    expect(refundCase.subAgentsInvoked).toEqual(['billing-specialist']);
    expect(lineageCase.traceId).toBe('tr-1');
  });
});

// ---------------------------------------------------------------------------
// evaluateChain — coverage aggregation
// ---------------------------------------------------------------------------

describe('evaluateChain — coverage', () => {
  it('aggregates sub-agent coverage across cases', async () => {
    const traces = [
      fakeTrace({ requestText: 'q1', subAgentEndpoints: ['billing'], traceId: 't-1' }),
      fakeTrace({ requestText: 'q2', subAgentEndpoints: ['billing'], traceId: 't-2' }),
      fakeTrace({ requestText: 'q3', subAgentEndpoints: ['technical'], traceId: 't-3' }),
    ];
    const evalset = [{ request: 'q1' }, { request: 'q2' }, { request: 'q3' }];

    const report = await evaluateChain(
      baseOpts({ evalset, traceSearch: makeTraceSearch(traces) }),
    );

    expect(report.subAgentCoverage).toEqual({ billing: 2, technical: 1 });
  });
});

// ---------------------------------------------------------------------------
// evaluateChain — cases without matching trace are dropped
// ---------------------------------------------------------------------------

describe('evaluateChain — missing trace', () => {
  it('drops evalset rows that have no matching trace (no crash)', async () => {
    const traces = [fakeTrace({ requestText: 'q1', traceId: 't-1' })];
    const evalset = [{ request: 'q1' }, { request: 'q2-no-trace' }];

    const report = await evaluateChain(
      baseOpts({ evalset, traceSearch: makeTraceSearch(traces) }),
    );

    expect(report.cases).toHaveLength(1);
    expect(report.cases[0].request).toBe('q1');
  });
});

// ---------------------------------------------------------------------------
// evaluateChain — forwards user_token / workspace_host / experiment
// ---------------------------------------------------------------------------

describe('evaluateChain — forwarding to evaluate()', () => {
  it('forwards userToken + workspaceHost + experiment to the inner predict_fn', async () => {
    const ml = makeMlflowEvaluate();
    const runner = makeRunner('ok');

    await evaluateChain(
      baseOpts({
        evalset: [{ request: 'q1' }],
        experiment: '/Users/me/agents/triage',
        userToken: 'obo-xyz',
        workspaceHost: 'https://workspace.cloud.databricks.com',
        mlflowEvaluate: ml.fn,
        runViaSDK: runner,
        traceSearch: makeTraceSearch([]),
      }),
    );

    // mlflowEvaluate is called once with our evalset.
    const args = ml.lastArgs();
    expect(args.data).toEqual([{ request: 'q1' }]);

    // Driving its predict_fn surfaces the OBO headers on the runner.
    await args.predict_fn({ request: 'q1' });
    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.oboHeaders).toEqual({
      Authorization: 'Bearer obo-xyz',
      'X-Databricks-Host': 'https://workspace.cloud.databricks.com',
    });
  });

  it('passes experiment to traceSearch under experimentNames', async () => {
    const ts = vi.fn(async () => []) as unknown as TraceSearchFn;
    await evaluateChain(
      baseOpts({
        evalset: [],
        experiment: '/Users/me/agents/triage',
        traceSearch: ts,
      }),
    );

    const tsCall = (ts as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(tsCall.experimentNames).toEqual(['/Users/me/agents/triage']);
    expect(tsCall.maxResults).toBe(50); // default
  });

  it('respects lookbackTraces override', async () => {
    const ts = vi.fn(async () => []) as unknown as TraceSearchFn;
    await evaluateChain(
      baseOpts({
        evalset: [],
        lookbackTraces: 7,
        traceSearch: ts,
      }),
    );
    const tsCall = (ts as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(tsCall.maxResults).toBe(7);
  });
});

// ---------------------------------------------------------------------------
// evaluateChain — traceSearch failure
// ---------------------------------------------------------------------------

describe('evaluateChain — traceSearch failures', () => {
  it('returns empty cases when traceSearch rejects (no throw)', async () => {
    const failingTs: TraceSearchFn = vi.fn(async () => {
      throw new Error('backend down');
    }) as unknown as TraceSearchFn;
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    const report = await evaluateChain(
      baseOpts({
        evalset: [{ request: 'q' }],
        traceSearch: failingTs,
      }),
    );

    expect(report.cases).toEqual([]);
    expect(report.subAgentCoverage).toEqual({});
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// evaluateChain — raw eval result + frozen report
// ---------------------------------------------------------------------------

describe('evaluateChain — return value', () => {
  it('returns the raw mlflowEvaluate result on the report', async () => {
    const sentinel = { metrics: { correctness: 0.9 } };
    const ml = makeMlflowEvaluate(sentinel);

    const report = await evaluateChain(
      baseOpts({
        evalset: [],
        mlflowEvaluate: ml.fn,
      }),
    );

    expect(report.rawEvalResult).toBe(sentinel);
  });

  it('returns a frozen report and frozen cases', async () => {
    const traces = [fakeTrace({ requestText: 'q1', subAgentEndpoints: ['billing'] })];
    const evalset = [{ request: 'q1' }];

    const report = await evaluateChain(
      baseOpts({ evalset, traceSearch: makeTraceSearch(traces) }),
    );

    expect(Object.isFrozen(report)).toBe(true);
    expect(Object.isFrozen(report.cases)).toBe(true);
    expect(Object.isFrozen(report.subAgentCoverage)).toBe(true);
    expect(Object.isFrozen(report.cases[0])).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// evaluateChain — validation
// ---------------------------------------------------------------------------

describe('evaluateChain — validation', () => {
  it('throws when model is empty', async () => {
    await expect(
      evaluateChain(baseOpts({ model: '' })),
    ).rejects.toThrow();
  });

  it('throws when experiment is empty', async () => {
    await expect(
      evaluateChain(baseOpts({ experiment: '' })),
    ).rejects.toThrow();
  });

  it('throws when mlflowEvaluate is missing', async () => {
    await expect(
      evaluateChain({
        agent: makeConfig(),
        model: 'm',
        evalset: [],
        experiment: '/x',
        // @ts-expect-error — intentionally missing
        mlflowEvaluate: undefined,
        traceSearch: makeTraceSearch([]),
      }),
    ).rejects.toThrow(/mlflowEvaluate/i);
  });

  it('throws when traceSearch is missing', async () => {
    await expect(
      evaluateChain({
        agent: makeConfig(),
        model: 'm',
        evalset: [],
        experiment: '/x',
        mlflowEvaluate: makeMlflowEvaluate().fn,
        // @ts-expect-error — intentionally missing
        traceSearch: undefined,
      }),
    ).rejects.toThrow(/traceSearch/i);
  });
});
