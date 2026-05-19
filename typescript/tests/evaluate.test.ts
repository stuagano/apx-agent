/**
 * Tests for evaluate() — TS port of python/tests/test_evaluate.py.
 *
 * Covers:
 *   1. extractMessages tolerates the various eval-row shapes
 *      (bare string, request/input/prompt/query/question, full messages).
 *   2. extractResponseText pulls the last assistant message content.
 *   3. evaluate() builds a predict_fn that returns the assistant text via
 *      the injected runViaSDK, forwards scorers + extra kwargs to the
 *      injected mlflowEvaluate, and threads userToken / workspaceHost
 *      through as OBO headers.
 *   4. Default scorers are used when none are supplied; an explicit empty
 *      list is preserved; experiment routing calls mlflowSetExperiment.
 *
 * mlflowEvaluate + runViaSDK + mlflowSetExperiment are all injected mocks
 * so tests run with no real workspace, mlflow, or FMAPI dependency.
 */

import { describe, it, expect, vi } from 'vitest';
import { z } from 'zod';
import {
  evaluate,
  extractMessages,
  extractResponseText,
  type EvalOptions,
  type MlflowEvaluateFn,
  type MlflowEvaluateArgs,
} from '../src/evaluate.js';
import type { RunViaSDKFn } from '../src/run-once.js';
import type { AgentConfig } from '../src/agent/plugin.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeConfig(overrides: Partial<AgentConfig> = {}): AgentConfig {
  return {
    name: 'test-agent',
    model: 'databricks-claude-sonnet-4-6',
    instructions: 'You are a test agent.',
    tools: [],
    ...overrides,
  };
}

function makeRunner(text = 'final assistant text'): RunViaSDKFn {
  return vi.fn(async () => text) as unknown as RunViaSDKFn;
}

/**
 * Make an mlflowEvaluate mock that captures call args. Returns the mock
 * (cast as MlflowEvaluateFn) plus a getter for the most recent args.
 */
function makeMlflowEvaluate(returnValue: unknown = { metrics: {} }): {
  fn: MlflowEvaluateFn;
  lastArgs: () => MlflowEvaluateArgs;
} {
  const mock = vi.fn(async () => returnValue);
  return {
    fn: mock as unknown as MlflowEvaluateFn,
    lastArgs: () => (mock as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as MlflowEvaluateArgs,
  };
}

function baseOpts(overrides: Partial<EvalOptions> = {}): EvalOptions {
  return {
    agent: makeConfig(),
    model: 'm',
    evalset: [],
    mlflowEvaluate: makeMlflowEvaluate().fn,
    runViaSDK: makeRunner(),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// extractMessages
// ---------------------------------------------------------------------------

describe('extractMessages', () => {
  it('handles bare strings', () => {
    expect(extractMessages('hello')).toEqual([{ role: 'user', content: 'hello' }]);
  });

  it.each(['request', 'input', 'prompt', 'query', 'question'])(
    'recognises common eval key %s',
    (key) => {
      const out = extractMessages({ [key]: "what's the lineage?" });
      expect(out).toEqual([{ role: 'user', content: "what's the lineage?" }]);
    },
  );

  it('passes through messages list', () => {
    const msgs = [
      { role: 'system', content: 'you are helpful' },
      { role: 'user', content: 'hi' },
    ];
    expect(extractMessages({ messages: msgs })).toEqual(msgs);
  });

  it('falls back to stringified input for unfamiliar shapes', () => {
    const out = extractMessages({ unknown_key: 'x' });
    expect(out[0].role).toBe('user');
    expect(out[0].content).toContain('unknown_key');
  });

  it('falls back to String(inputs) for non-object non-string inputs', () => {
    const out = extractMessages(42);
    expect(out).toEqual([{ role: 'user', content: '42' }]);
  });

  it('coerces non-string prompt-key values via String()', () => {
    const out = extractMessages({ request: 123 });
    expect(out).toEqual([{ role: 'user', content: '123' }]);
  });

  it('skips null/undefined prompt-key values', () => {
    // request is null → fall through; should not produce a "null" content.
    const out = extractMessages({ request: null, input: 'fallback' });
    expect(out).toEqual([{ role: 'user', content: 'fallback' }]);
  });
});

// ---------------------------------------------------------------------------
// extractResponseText
// ---------------------------------------------------------------------------

describe('extractResponseText', () => {
  it('returns last assistant content', () => {
    const response = {
      messages: [
        { role: 'user', content: 'hi' },
        { role: 'assistant', content: 'hello there' },
      ],
    };
    expect(extractResponseText(response)).toBe('hello there');
  });

  it('walks from the end, skipping intermediate non-assistant messages', () => {
    const response = {
      messages: [
        { role: 'assistant', content: 'intermediate' },
        { role: 'tool', content: 'tool result' },
        { role: 'assistant', content: 'final' },
      ],
    };
    expect(extractResponseText(response)).toBe('final');
  });

  it('returns empty string when no assistant message present', () => {
    const response = { messages: [{ role: 'user', content: 'x' }] };
    expect(extractResponseText(response)).toBe('');
  });

  it('returns empty string when messages list is empty', () => {
    expect(extractResponseText({ messages: [] })).toBe('');
  });

  it('returns empty string when messages key is missing', () => {
    expect(extractResponseText({})).toBe('');
  });

  it('returns empty string when input is not an object', () => {
    expect(extractResponseText(null)).toBe('');
    expect(extractResponseText(undefined)).toBe('');
    expect(extractResponseText('not an object')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// evaluate — predict_fn + mlflowEvaluate wiring
// ---------------------------------------------------------------------------

describe('evaluate — predict_fn wiring', () => {
  it('builds a predict_fn that invokes runViaSDK and returns the final text', async () => {
    const runner = makeRunner('the lineage is upstream.gold.orders');
    const ml = makeMlflowEvaluate();

    await evaluate(
      baseOpts({
        evalset: [{ request: 'what is the lineage?' }],
        scorers: [],
        runViaSDK: runner,
        mlflowEvaluate: ml.fn,
      }),
    );

    // mlflowEvaluate was called exactly once with our evalset + scorers
    expect((ml.fn as unknown as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(1);
    const args = ml.lastArgs();
    expect(args.data).toEqual([{ request: 'what is the lineage?' }]);
    expect(args.scorers).toEqual([]);

    // Invoke the predict_fn the way mlflow would
    const out = await args.predict_fn({ request: 'what is the lineage?' });
    expect(out).toBe('the lineage is upstream.gold.orders');

    // runViaSDK received exactly one user message with our request content
    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.messages).toEqual([
      { role: 'user', content: 'what is the lineage?' },
    ]);
  });

  it('forwards model + instructions + tools from the agent config', async () => {
    const runner = makeRunner();
    const ml = makeMlflowEvaluate();
    const cfg = makeConfig({
      model: 'cfg-model-ignored-because-opts-model-wins',
      instructions: 'be concise',
    });

    await evaluate(
      baseOpts({
        agent: cfg,
        model: 'opts-model',
        scorers: [],
        runViaSDK: runner,
        mlflowEvaluate: ml.fn,
      }),
    );

    await ml.lastArgs().predict_fn('hi');

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.model).toBe('opts-model');
    expect(runnerCall.instructions).toBe('be concise');
    expect(runnerCall.tools).toEqual([]);
  });

  it('throws ZodError when model is empty', async () => {
    await expect(
      evaluate(baseOpts({ model: '' })),
    ).rejects.toBeInstanceOf(z.ZodError);
  });

  it('throws when mlflowEvaluate is missing', async () => {
    await expect(
      evaluate({
        agent: makeConfig(),
        model: 'm',
        evalset: [],
        // @ts-expect-error — intentionally missing for the test
        mlflowEvaluate: undefined,
      }),
    ).rejects.toThrow(/mlflowEvaluate/i);
  });
});

// ---------------------------------------------------------------------------
// evaluate — OBO token threading
// ---------------------------------------------------------------------------

describe('evaluate — OBO threading', () => {
  it('threads userToken + workspaceHost as oboHeaders', async () => {
    const runner = makeRunner('ok');
    const ml = makeMlflowEvaluate();

    await evaluate(
      baseOpts({
        scorers: [],
        userToken: 'user-obo-token-xyz',
        workspaceHost: 'https://my-workspace.cloud.databricks.com',
        runViaSDK: runner,
        mlflowEvaluate: ml.fn,
      }),
    );

    await ml.lastArgs().predict_fn('hi');

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.oboHeaders).toEqual({
      Authorization: 'Bearer user-obo-token-xyz',
      'X-Databricks-Host': 'https://my-workspace.cloud.databricks.com',
    });
  });

  it('threads userToken alone (no workspaceHost)', async () => {
    const runner = makeRunner('ok');
    const ml = makeMlflowEvaluate();

    await evaluate(
      baseOpts({
        scorers: [],
        userToken: 'tok',
        runViaSDK: runner,
        mlflowEvaluate: ml.fn,
      }),
    );

    await ml.lastArgs().predict_fn('hi');

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.oboHeaders).toEqual({ Authorization: 'Bearer tok' });
  });

  it('passes empty oboHeaders when userToken is omitted', async () => {
    const runner = makeRunner('ok');
    const ml = makeMlflowEvaluate();

    await evaluate(
      baseOpts({
        scorers: [],
        runViaSDK: runner,
        mlflowEvaluate: ml.fn,
      }),
    );

    await ml.lastArgs().predict_fn('hi');

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.oboHeaders).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// evaluate — mlflowKwargs pass-through
// ---------------------------------------------------------------------------

describe('evaluate — mlflowKwargs forwarding', () => {
  it('forwards extra mlflowKwargs verbatim to mlflowEvaluate', async () => {
    const ml = makeMlflowEvaluate();

    await evaluate(
      baseOpts({
        scorers: [],
        mlflowKwargs: {
          experiment_id: 'exp-123',
          model_id: 'model-abc',
        },
        mlflowEvaluate: ml.fn,
      }),
    );

    const args = ml.lastArgs();
    expect(args.experiment_id).toBe('exp-123');
    expect(args.model_id).toBe('model-abc');
  });
});

// ---------------------------------------------------------------------------
// evaluate — scorer trichotomy
// ---------------------------------------------------------------------------

describe('evaluate — scorer resolution', () => {
  it('uses defaultScorers when scorers is undefined', async () => {
    const ml = makeMlflowEvaluate();
    const sentinel = ['correctness-sentinel', 'relevance-sentinel'];

    await evaluate(
      baseOpts({
        // scorers omitted entirely
        defaultScorers: sentinel,
        mlflowEvaluate: ml.fn,
      }),
    );

    expect(ml.lastArgs().scorers).toBe(sentinel);
  });

  it('omits scorers key when both scorers and defaultScorers are undefined', async () => {
    const ml = makeMlflowEvaluate();

    await evaluate(
      baseOpts({
        // scorers omitted, no defaultScorers
        mlflowEvaluate: ml.fn,
      }),
    );

    const args = ml.lastArgs();
    expect('scorers' in args).toBe(false);
  });

  it('preserves explicit empty scorers list (not defaults)', async () => {
    const ml = makeMlflowEvaluate();

    await evaluate(
      baseOpts({
        scorers: [],
        defaultScorers: ['should-not-be-used'],
        mlflowEvaluate: ml.fn,
      }),
    );

    expect(ml.lastArgs().scorers).toEqual([]);
  });

  it('preserves explicit non-empty scorers list', async () => {
    const ml = makeMlflowEvaluate();
    const scorers = ['custom-scorer'];

    await evaluate(
      baseOpts({
        scorers,
        defaultScorers: ['should-not-be-used'],
        mlflowEvaluate: ml.fn,
      }),
    );

    expect(ml.lastArgs().scorers).toBe(scorers);
  });
});

// ---------------------------------------------------------------------------
// evaluate — experiment routing
// ---------------------------------------------------------------------------

describe('evaluate — experiment routing', () => {
  it('calls mlflowSetExperiment when experiment + setter are both set', async () => {
    const ml = makeMlflowEvaluate();
    const setExp = vi.fn();

    await evaluate(
      baseOpts({
        scorers: [],
        experiment: '/Users/me/agents/triage',
        mlflowSetExperiment: setExp,
        mlflowEvaluate: ml.fn,
      }),
    );

    expect(setExp).toHaveBeenCalledTimes(1);
    expect(setExp).toHaveBeenCalledWith('/Users/me/agents/triage');
  });

  it('skips mlflowSetExperiment when experiment is omitted', async () => {
    const ml = makeMlflowEvaluate();
    const setExp = vi.fn();

    await evaluate(
      baseOpts({
        scorers: [],
        mlflowSetExperiment: setExp,
        mlflowEvaluate: ml.fn,
      }),
    );

    expect(setExp).not.toHaveBeenCalled();
  });

  it('warns + continues when experiment is set but no setter is provided', async () => {
    const ml = makeMlflowEvaluate();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    await evaluate(
      baseOpts({
        scorers: [],
        experiment: '/Users/me/agents/triage',
        mlflowEvaluate: ml.fn,
      }),
    );

    expect(warnSpy).toHaveBeenCalled();
    expect((ml.fn as unknown as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(1);
    warnSpy.mockRestore();
  });

  it('wraps mlflowSetExperiment failures with a friendly error', async () => {
    const setExp = vi.fn(() => {
      throw new Error('bad path');
    });

    await expect(
      evaluate(
        baseOpts({
          scorers: [],
          experiment: '/bad/path',
          mlflowSetExperiment: setExp,
        }),
      ),
    ).rejects.toThrow(/mlflowSetExperiment/);
  });
});

// ---------------------------------------------------------------------------
// evaluate — return value
// ---------------------------------------------------------------------------

describe('evaluate — return value', () => {
  it('returns whatever mlflowEvaluate returns', async () => {
    const sentinel = { metrics: { correctness: 0.9 } };
    const ml = makeMlflowEvaluate(sentinel);

    const result = await evaluate(
      baseOpts({
        scorers: [],
        mlflowEvaluate: ml.fn,
      }),
    );

    expect(result).toBe(sentinel);
  });
});
