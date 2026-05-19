/**
 * Tests for chat-agent.ts — TS port of python/tests/test_chat_agent.py.
 *
 * Adapted because TS has different load-bearing surfaces:
 *   - Python's load-bearing test asserts that `user_token` in custom_inputs
 *     flows to the per-request WorkspaceClient. TS has no WorkspaceClient
 *     factory in this path; the equivalent assertion is that
 *     `customInputs.user_token` + `workspace_host` are translated to
 *     `Authorization: Bearer ...` + `X-Databricks-Host` headers on the
 *     `runViaSDK` call.
 *
 * Covers:
 *   1. compileToChatAgent returns predict + predictStream functions.
 *   2. predict invokes runViaSDK with the right model + messages.
 *   3. Session prepending: history is prepended when sessionStore + id set.
 *   4. Session persistence: input + assistant messages are stored after predict.
 *   5. userToken / workspaceHost → oboHeaders translation.
 *   6. predict returns ONLY the new (assistant) message, not the user echo.
 *   7. predictStream yields one chunk (the documented streaming-gap fallback).
 *   8. logAgent calls mlflowLogModel with auto-derived resources.
 *   9. logAgent calls mlflowSetExperiment when both experiment + setter set.
 *  10. logAgent skips setExperiment when experiment is omitted.
 *  11. logAgent throws when mlflowLogModel is missing.
 *  12. materializeResources callable used when provided.
 */

import { describe, it, expect, vi } from 'vitest';
import { z } from 'zod';
import {
  compileToChatAgent,
  logAgent,
  type ChatAgentMessage,
  type MlflowLogModelFn,
  type MlflowLogModelArgs,
} from '../src/chat-agent.js';
import {
  InMemorySessionStore,
  Session,
  type SessionStore,
} from '../src/workflows/session.js';
import type { RunViaSDKFn } from '../src/run-once.js';
import type { AgentConfig } from '../src/agent/plugin.js';
import { attachResources, makeResourceSpec, type ResourceSpec } from '../src/resources.js';

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

function makeRunner(text = 'final answer'): RunViaSDKFn {
  return vi.fn(async () => text) as unknown as RunViaSDKFn;
}

function makeUserMessage(content: string, id = 'u1'): ChatAgentMessage {
  return { role: 'user', content, id };
}

function makeLogModelFn(returnValue: unknown = { modelInfo: 'ok' }): {
  fn: MlflowLogModelFn;
  lastArgs: () => MlflowLogModelArgs;
} {
  const mock = vi.fn(async () => returnValue);
  return {
    fn: mock as unknown as MlflowLogModelFn,
    lastArgs: () => (mock as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0] as MlflowLogModelArgs,
  };
}

// Default no-op iterators (no resources to collect from the agent itself —
// only the `model` endpoint comes through).
const emptyToolIterator = () => [] as unknown[];
const emptySubAgentIterator = () => [] as string[];

// ---------------------------------------------------------------------------
// compileToChatAgent — shape
// ---------------------------------------------------------------------------

describe('compileToChatAgent — shape', () => {
  it('returns predict + predictStream functions', () => {
    const chat = compileToChatAgent({ agent: makeConfig(), model: 'm' });
    expect(typeof chat.predict).toBe('function');
    expect(typeof chat.predictStream).toBe('function');
  });

  it('throws ZodError when model is empty', () => {
    expect(() =>
      compileToChatAgent({ agent: makeConfig(), model: '' }),
    ).toThrow(z.ZodError);
  });
});

// ---------------------------------------------------------------------------
// predict — runner invocation
// ---------------------------------------------------------------------------

describe('predict — runner invocation', () => {
  it('invokes runViaSDK with model + user message', async () => {
    const runner = makeRunner('hello back');
    const chat = compileToChatAgent({
      agent: makeConfig({ instructions: 'be helpful' }),
      model: 'my-endpoint',
      runViaSDK: runner,
    });

    await chat.predict({ messages: [makeUserMessage('hi')] });

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.model).toBe('my-endpoint');
    expect(runnerCall.instructions).toBe('be helpful');
    expect(runnerCall.messages).toEqual([{ role: 'user', content: 'hi' }]);
  });

  it('returns only the new assistant message (not the user echo)', async () => {
    const runner = makeRunner('final answer');
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });

    const resp = await chat.predict({ messages: [makeUserMessage('hi')] });

    expect(resp.messages).toHaveLength(1);
    expect(resp.messages[0].role).toBe('assistant');
    expect(resp.messages[0].content).toBe('final answer');
  });
});

// ---------------------------------------------------------------------------
// predict — session prepending + persistence
// ---------------------------------------------------------------------------

describe('predict — session prepending', () => {
  it('prepends history when sessionStore + session_id are set', async () => {
    const store: SessionStore = new InMemorySessionStore();

    // Seed a session with a prior turn.
    const seed = new Session({ id: 'sess-1', store });
    seed.addMessage('user', 'previous question');
    seed.addMessage('assistant', 'previous answer');
    await seed.save();

    const runner = makeRunner('new answer');
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      sessionStore: store,
      runViaSDK: runner,
    });

    await chat.predict({
      messages: [makeUserMessage('current question')],
      customInputs: { session_id: 'sess-1' },
    });

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.messages).toEqual([
      { role: 'user', content: 'previous question' },
      { role: 'assistant', content: 'previous answer' },
      { role: 'user', content: 'current question' },
    ]);
  });

  it('does not prepend when sessionStore is unset', async () => {
    const runner = makeRunner('ans');
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });

    await chat.predict({
      messages: [makeUserMessage('only message')],
      customInputs: { session_id: 'sess-1' },
    });

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.messages).toEqual([{ role: 'user', content: 'only message' }]);
  });

  it('does not prepend when session_id is missing from customInputs', async () => {
    const store: SessionStore = new InMemorySessionStore();
    const runner = makeRunner('ans');
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      sessionStore: store,
      runViaSDK: runner,
    });

    await chat.predict({ messages: [makeUserMessage('hi')] });

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.messages).toEqual([{ role: 'user', content: 'hi' }]);
  });
});

describe('predict — session persistence', () => {
  it('persists user input + assistant reply after predict', async () => {
    const store: SessionStore = new InMemorySessionStore();
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      sessionStore: store,
      runViaSDK: makeRunner('assistant says hi'),
    });

    await chat.predict({
      messages: [makeUserMessage('user says hi')],
      customInputs: { session_id: 'sess-2' },
    });

    const reloaded = await Session.load('sess-2', store);
    expect(reloaded).not.toBeNull();
    const history = reloaded!.getHistory();
    expect(history).toEqual([
      { role: 'user', content: 'user says hi' },
      { role: 'assistant', content: 'assistant says hi' },
    ]);
  });

  it('appends across multiple turns', async () => {
    const store: SessionStore = new InMemorySessionStore();
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      sessionStore: store,
      runViaSDK: makeRunner('a1'),
    });

    await chat.predict({
      messages: [makeUserMessage('q1')],
      customInputs: { session_id: 'sess-3' },
    });
    await chat.predict({
      messages: [makeUserMessage('q2')],
      customInputs: { session_id: 'sess-3' },
    });

    const reloaded = await Session.load('sess-3', store);
    expect(reloaded!.getHistory()).toEqual([
      { role: 'user', content: 'q1' },
      { role: 'assistant', content: 'a1' },
      { role: 'user', content: 'q2' },
      { role: 'assistant', content: 'a1' },
    ]);
  });
});

// ---------------------------------------------------------------------------
// predict — OBO header translation
// ---------------------------------------------------------------------------

describe('predict — OBO header translation', () => {
  it('translates user_token + workspace_host to oboHeaders', async () => {
    const runner = makeRunner();
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });

    await chat.predict({
      messages: [makeUserMessage('hi')],
      customInputs: {
        user_token: 'tok-xyz',
        workspace_host: 'https://wsp.cloud.databricks.com',
      },
    });

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.oboHeaders).toEqual({
      Authorization: 'Bearer tok-xyz',
      'X-Databricks-Host': 'https://wsp.cloud.databricks.com',
    });
  });

  it('translates user_token alone (no workspace_host)', async () => {
    const runner = makeRunner();
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });

    await chat.predict({
      messages: [makeUserMessage('hi')],
      customInputs: { user_token: 'tok' },
    });

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.oboHeaders).toEqual({ Authorization: 'Bearer tok' });
  });

  it('passes empty oboHeaders when no user_token', async () => {
    const runner = makeRunner();
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });

    await chat.predict({ messages: [makeUserMessage('hi')] });

    const runnerCall = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(runnerCall.oboHeaders).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// predictStream — documented single-chunk fallback
// ---------------------------------------------------------------------------

describe('predictStream — single-chunk fallback', () => {
  it('yields one chunk containing the assistant message', async () => {
    const chat = compileToChatAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: makeRunner('streamed!'),
    });

    const chunks: Array<{ delta: ChatAgentMessage }> = [];
    for await (const chunk of chat.predictStream({ messages: [makeUserMessage('go')] })) {
      chunks.push(chunk);
    }

    expect(chunks).toHaveLength(1);
    expect(chunks[0].delta.role).toBe('assistant');
    expect(chunks[0].delta.content).toBe('streamed!');
  });
});

// ---------------------------------------------------------------------------
// logAgent — orchestration
// ---------------------------------------------------------------------------

describe('logAgent — basic logging', () => {
  it('calls mlflowLogModel with auto-derived resources (model endpoint)', async () => {
    const log = makeLogModelFn();

    await logAgent({
      agent: makeConfig(),
      model: 'my-endpoint',
      mlflowLogModel: log.fn,
      toolIterator: emptyToolIterator,
      subAgentIterator: emptySubAgentIterator,
    });

    const args = log.lastArgs();
    expect(args.artifactPath).toBe('agent');
    expect(args.pythonModel).toBeTypeOf('object');
    expect(args.resources).toEqual([
      { kind: 'serving_endpoint', identifier: 'my-endpoint' },
    ]);
  });

  it('forwards registeredModelName + artifactPath when set', async () => {
    const log = makeLogModelFn();

    await logAgent({
      agent: makeConfig(),
      model: 'm',
      registeredModelName: 'main.agents.triage',
      artifactPath: 'custom-path',
      mlflowLogModel: log.fn,
      toolIterator: emptyToolIterator,
      subAgentIterator: emptySubAgentIterator,
    });

    const args = log.lastArgs();
    expect(args.registeredModelName).toBe('main.agents.triage');
    expect(args.artifactPath).toBe('custom-path');
  });

  it('collects tool-attached resources via toolIterator', async () => {
    const log = makeLogModelFn();
    const tool = (): string => 'noop';
    attachResources(tool, [makeResourceSpec('uc_table', 'main.sales.orders')]);

    await logAgent({
      agent: makeConfig(),
      model: 'm',
      mlflowLogModel: log.fn,
      toolIterator: () => [tool],
      subAgentIterator: emptySubAgentIterator,
    });

    const args = log.lastArgs();
    expect(args.resources).toEqual([
      { kind: 'serving_endpoint', identifier: 'm' },
      { kind: 'uc_table', identifier: 'main.sales.orders' },
    ]);
  });

  it('returns whatever mlflowLogModel returns', async () => {
    const sentinel = { registeredModelVersion: '7', modelInfo: { name: 'x' } };
    const log = makeLogModelFn(sentinel);

    const result = await logAgent({
      agent: makeConfig(),
      model: 'm',
      mlflowLogModel: log.fn,
      toolIterator: emptyToolIterator,
      subAgentIterator: emptySubAgentIterator,
    });

    expect(result).toBe(sentinel);
  });
});

describe('logAgent — experiment routing', () => {
  it('calls mlflowSetExperiment when experiment + setter both set', async () => {
    const log = makeLogModelFn();
    const setExp = vi.fn();

    await logAgent({
      agent: makeConfig(),
      model: 'm',
      experiment: '/Users/me/agents/triage',
      mlflowLogModel: log.fn,
      mlflowSetExperiment: setExp,
      toolIterator: emptyToolIterator,
      subAgentIterator: emptySubAgentIterator,
    });

    expect(setExp).toHaveBeenCalledTimes(1);
    expect(setExp).toHaveBeenCalledWith('/Users/me/agents/triage');
  });

  it('skips mlflowSetExperiment when experiment is omitted', async () => {
    const log = makeLogModelFn();
    const setExp = vi.fn();

    await logAgent({
      agent: makeConfig(),
      model: 'm',
      mlflowLogModel: log.fn,
      mlflowSetExperiment: setExp,
      toolIterator: emptyToolIterator,
      subAgentIterator: emptySubAgentIterator,
    });

    expect(setExp).not.toHaveBeenCalled();
  });

  it('warns + continues when experiment is set but no setter', async () => {
    const log = makeLogModelFn();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    await logAgent({
      agent: makeConfig(),
      model: 'm',
      experiment: '/Users/me/agents/triage',
      mlflowLogModel: log.fn,
      toolIterator: emptyToolIterator,
      subAgentIterator: emptySubAgentIterator,
    });

    expect(warnSpy).toHaveBeenCalled();
    expect((log.fn as unknown as ReturnType<typeof vi.fn>).mock.calls).toHaveLength(1);
    warnSpy.mockRestore();
  });

  it('wraps mlflowSetExperiment failures with a friendly error', async () => {
    const log = makeLogModelFn();
    const setExp = vi.fn(() => {
      throw new Error('bad path');
    });

    await expect(
      logAgent({
        agent: makeConfig(),
        model: 'm',
        experiment: '/bad/path',
        mlflowLogModel: log.fn,
        mlflowSetExperiment: setExp,
        toolIterator: emptyToolIterator,
        subAgentIterator: emptySubAgentIterator,
      }),
    ).rejects.toThrow(/mlflowSetExperiment/);
  });
});

describe('logAgent — validation', () => {
  it('throws ZodError when model is empty', async () => {
    await expect(
      logAgent({
        agent: makeConfig(),
        model: '',
        mlflowLogModel: makeLogModelFn().fn,
        toolIterator: emptyToolIterator,
        subAgentIterator: emptySubAgentIterator,
      }),
    ).rejects.toBeInstanceOf(z.ZodError);
  });

  it('throws when mlflowLogModel is missing', async () => {
    await expect(
      logAgent({
        agent: makeConfig(),
        model: 'm',
        // @ts-expect-error — intentionally missing for the test
        mlflowLogModel: undefined,
        toolIterator: emptyToolIterator,
        subAgentIterator: emptySubAgentIterator,
      }),
    ).rejects.toThrow(/mlflowLogModel/i);
  });

  it('throws when toolIterator is missing', async () => {
    await expect(
      logAgent({
        agent: makeConfig(),
        model: 'm',
        mlflowLogModel: makeLogModelFn().fn,
        // @ts-expect-error — intentionally missing for the test
        toolIterator: undefined,
        subAgentIterator: emptySubAgentIterator,
      }),
    ).rejects.toThrow(/toolIterator/i);
  });
});

describe('logAgent — materializeResources injection', () => {
  it('uses materializeResources when provided', async () => {
    const log = makeLogModelFn();
    const materialised: Array<{ kind: string; identifier: string; concrete: true }> = [];
    const materialize = vi.fn((specs: ResourceSpec[]) =>
      specs.map((s) => {
        const obj = { kind: s.kind, identifier: s.identifier, concrete: true as const };
        materialised.push(obj);
        return obj;
      }),
    );

    await logAgent({
      agent: makeConfig(),
      model: 'my-model',
      mlflowLogModel: log.fn,
      toolIterator: emptyToolIterator,
      subAgentIterator: emptySubAgentIterator,
      materializeResources: materialize,
    });

    // Materialiser was called once with the auto-derived ResourceSpec list.
    expect(materialize).toHaveBeenCalledTimes(1);
    const calledWith = materialize.mock.calls[0][0];
    expect(calledWith).toEqual([
      { kind: 'serving_endpoint', identifier: 'my-model' },
    ]);

    // And the materialised objects (not raw specs) were forwarded.
    const args = log.lastArgs();
    expect(args.resources).toEqual([
      { kind: 'serving_endpoint', identifier: 'my-model', concrete: true },
    ]);
  });

  it('forwards raw ResourceSpecs when materializeResources is omitted', async () => {
    const log = makeLogModelFn();

    await logAgent({
      agent: makeConfig(),
      model: 'm',
      mlflowLogModel: log.fn,
      toolIterator: emptyToolIterator,
      subAgentIterator: emptySubAgentIterator,
    });

    const args = log.lastArgs();
    // No materialiser → raw ResourceSpec objects (frozen but plain-shape).
    expect(args.resources).toEqual([
      { kind: 'serving_endpoint', identifier: 'm' },
    ]);
  });
});
