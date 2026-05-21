/**
 * Tests for responses-agent.ts — TS port of python/tests/test_responses_agent.py.
 *
 * The Python tests assert that:
 *   - compile_to_responses_agent returns (non_streaming, streaming) callables
 *   - non_streaming round-trips a request through the compiled graph
 *   - streaming yields output_item.done + response.completed events
 *   - OBO context (user_token in custom_inputs OR X-Forwarded-Access-Token
 *     header) flows through to the WorkspaceClient
 *   - thread_id in custom_inputs prepends session history
 *
 * The TS port mirrors these but adapts the assertion surface to the
 * injectable runner: instead of asserting on a WorkspaceClient, we assert
 * that the right oboHeaders bag was handed to runViaSDK.
 */

import { describe, it, expect, vi } from 'vitest';

import {
  compileToResponsesAgent,
  mountResponsesAgent,
  type ResponsesAgentRequest,
  type ResponsesAgentStreamEvent,
} from '../src/responses-agent.js';
import { InMemorySessionStore } from '../src/workflows/session.js';
import type { RunViaSDKFn, StreamViaSDKFn } from '../src/run-once.js';
import type { AgentConfig } from '../src/agent/plugin.js';

function makeConfig(overrides: Partial<AgentConfig> = {}): AgentConfig {
  return {
    name: 'test-agent',
    model: 'databricks-claude-sonnet-4-6',
    instructions: 'You are a test agent.',
    tools: [],
    ...overrides,
  };
}

function makeRunner(text = 'final answer'): { runner: RunViaSDKFn; lastParams: () => any } {
  const mock = vi.fn(async () => text);
  return {
    runner: mock as unknown as RunViaSDKFn,
    lastParams: () => (mock.mock.calls[0]?.[0] ?? {}) as Record<string, unknown>,
  };
}

function makeStreamer(chunks: string[]): { streamer: StreamViaSDKFn; lastParams: () => any } {
  const mock = vi.fn(async function* () {
    for (const c of chunks) yield c;
  });
  return {
    streamer: mock as unknown as StreamViaSDKFn,
    lastParams: () => (mock.mock.calls[0]?.[0] ?? {}) as Record<string, unknown>,
  };
}

function userTurn(content: string): ResponsesAgentRequest {
  return { input: [{ role: 'user', content }] };
}

// ---------------------------------------------------------------------------

describe('compileToResponsesAgent — shape', () => {
  it('returns invoke + stream functions', () => {
    const agent = compileToResponsesAgent({ agent: makeConfig(), model: 'm' });
    expect(typeof agent.invoke).toBe('function');
    expect(typeof agent.stream).toBe('function');
  });

  it('rejects empty model string', () => {
    expect(() =>
      compileToResponsesAgent({ agent: makeConfig(), model: '' }),
    ).toThrow();
  });
});

describe('compileToResponsesAgent — invoke', () => {
  it('returns a ResponsesAgentResponse with a single message output item', async () => {
    const { runner } = makeRunner('hello!');
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });
    const response = await agent.invoke(userTurn('hi'));
    expect(response.output).toHaveLength(1);
    expect(response.output[0]).toMatchObject({
      type: 'message',
      role: 'assistant',
      status: 'completed',
    });
    const content = response.output[0]['content'] as Array<{ text: string }>;
    expect(content[0].text).toBe('hello!');
  });

  it('forwards model + messages to the runner', async () => {
    const { runner, lastParams } = makeRunner();
    const agent = compileToResponsesAgent({
      agent: makeConfig({ instructions: 'Be terse.' }),
      model: 'databricks-llama-3-1-405b-instruct',
      runViaSDK: runner,
    });
    await agent.invoke(userTurn('hello'));
    const params = lastParams();
    expect(params.model).toBe('databricks-llama-3-1-405b-instruct');
    expect(params.instructions).toBe('Be terse.');
    expect(params.messages).toEqual([{ role: 'user', content: 'hello' }]);
  });

  it('extracts text from array-shaped content', async () => {
    const { runner, lastParams } = makeRunner();
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });
    await agent.invoke({
      input: [
        {
          role: 'user',
          content: [
            { type: 'input_text', text: 'part-a ' },
            { type: 'input_text', text: 'part-b' },
          ],
        },
      ],
    });
    const params = lastParams();
    expect(params.messages[0].content).toBe('part-a part-b');
  });
});

describe('compileToResponsesAgent — OBO header translation', () => {
  it('translates user_token in customInputs to Authorization Bearer', async () => {
    const { runner, lastParams } = makeRunner();
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });
    await agent.invoke({
      input: [{ role: 'user', content: 'hi' }],
      customInputs: { user_token: 'tok-abc', workspace_host: 'h.databricks.com' },
    });
    const params = lastParams();
    expect(params.oboHeaders).toEqual({
      Authorization: 'Bearer tok-abc',
      'X-Databricks-Host': 'h.databricks.com',
    });
  });

  it('translates X-Forwarded-Access-Token header (Apps proxy path)', async () => {
    const { runner, lastParams } = makeRunner();
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });
    await agent.invoke({
      input: [{ role: 'user', content: 'hi' }],
      headers: {
        'x-forwarded-access-token': 'proxy-tok',
        'x-forwarded-host': 'proxy-host',
      },
    });
    const params = lastParams();
    expect(params.oboHeaders.Authorization).toBe('Bearer proxy-tok');
    expect(params.oboHeaders['X-Databricks-Host']).toBe('proxy-host');
  });

  it('customInputs.user_token wins over header (caller-supplied beats proxy)', async () => {
    const { runner, lastParams } = makeRunner();
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });
    await agent.invoke({
      input: [{ role: 'user', content: 'hi' }],
      customInputs: { user_token: 'caller' },
      headers: { 'x-forwarded-access-token': 'proxy' },
    });
    const params = lastParams();
    expect(params.oboHeaders.Authorization).toBe('Bearer caller');
  });

  it('emits no oboHeaders when no token source is available', async () => {
    const { runner, lastParams } = makeRunner();
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });
    await agent.invoke(userTurn('hi'));
    expect(lastParams().oboHeaders).toEqual({});
  });
});

describe('compileToResponsesAgent — session prepending', () => {
  it('prepends history when thread_id is set', async () => {
    const store = new InMemorySessionStore();
    const { runner, lastParams } = makeRunner('reply');
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      sessionStore: store,
      runViaSDK: runner,
    });
    // First turn — establishes the session.
    await agent.invoke({
      input: [{ role: 'user', content: 'first' }],
      customInputs: { thread_id: 't1' },
    });
    // Second turn — history should be prepended.
    await agent.invoke({
      input: [{ role: 'user', content: 'second' }],
      customInputs: { thread_id: 't1' },
    });
    const params = (runner as unknown as { mock: { calls: any[] } }).mock.calls[1][0] as {
      messages: Array<{ role: string; content: string }>;
    };
    expect(params.messages.length).toBeGreaterThan(1);
    expect(params.messages[0]).toEqual({ role: 'user', content: 'first' });
    expect(params.messages[1]).toEqual({ role: 'assistant', content: 'reply' });
    expect(params.messages[2]).toEqual({ role: 'user', content: 'second' });
    void lastParams;
  });

  it('also accepts session_id (symmetry with chat-agent)', async () => {
    const store = new InMemorySessionStore();
    const { runner } = makeRunner('reply');
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      sessionStore: store,
      runViaSDK: runner,
    });
    await agent.invoke({
      input: [{ role: 'user', content: 'first' }],
      customInputs: { session_id: 's-1' },
    });
    const snap = await store.load('s-1');
    expect(snap).not.toBeNull();
    expect((snap?.messages ?? []).length).toBeGreaterThanOrEqual(2);
  });
});

describe('compileToResponsesAgent — stream', () => {
  it('yields output_item.done followed by response.completed', async () => {
    const { streamer } = makeStreamer(['Hel', 'lo!']);
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      streamViaSDK: streamer,
    });
    const events: ResponsesAgentStreamEvent[] = [];
    for await (const ev of agent.stream(userTurn('hi'))) {
      events.push(ev);
    }
    expect(events.length).toBeGreaterThanOrEqual(2);
    expect(events[0].type).toBe('response.output_item.done');
    expect(events[events.length - 1].type).toBe('response.completed');

    const item = events[0]['item'] as { content: Array<{ text: string }> };
    expect(item.content[0].text).toBe('Hello!');
  });

  it('translates OBO context the same way as invoke', async () => {
    const { streamer, lastParams } = makeStreamer(['x']);
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      streamViaSDK: streamer,
    });
    const iter = agent.stream({
      input: [{ role: 'user', content: 'hi' }],
      customInputs: { user_token: 'tok-s' },
    });
    for await (const _ of iter) void _;
    expect(lastParams().oboHeaders.Authorization).toBe('Bearer tok-s');
  });
});

describe('mountResponsesAgent — Express mounting', () => {
  it('returns false when the agent is missing invoke', () => {
    const app = { post: vi.fn() };
    const ok = mountResponsesAgent({
      app,
      agent: {} as never,
    });
    expect(ok).toBe(false);
  });

  it('mounts to /invocations + /api/invocations by default', () => {
    const app = { post: vi.fn() };
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: makeRunner().runner,
    });
    const ok = mountResponsesAgent({ app, agent });
    expect(ok).toBe(true);
    const paths = app.post.mock.calls.map((c) => c[0]);
    expect(paths).toContain('/invocations');
    expect(paths).toContain('/api/invocations');
  });

  it('rejects requests without an input array', async () => {
    const app = { post: vi.fn() };
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: makeRunner().runner,
    });
    mountResponsesAgent({ app, agent });
    const [, handler] = app.post.mock.calls[0];

    const res = makeResponseStub();
    await handler({ body: {}, headers: {} }, res);
    expect(res.statusCode).toBe(400);
    expect(res.jsonBody?.error).toMatch(/input/);
  });

  it('round-trips a non-streaming request', async () => {
    const app = { post: vi.fn() };
    const { runner } = makeRunner('hi back');
    const agent = compileToResponsesAgent({
      agent: makeConfig(),
      model: 'm',
      runViaSDK: runner,
    });
    mountResponsesAgent({ app, agent });
    const [, handler] = app.post.mock.calls[0];

    const res = makeResponseStub();
    await handler(
      { body: { input: [{ role: 'user', content: 'hi' }] }, headers: {} },
      res,
    );
    expect(res.statusCode).toBe(200);
    const out = res.jsonBody as { output: Array<{ content: Array<{ text: string }> }> };
    expect(out.output[0].content[0].text).toBe('hi back');
  });
});

function makeResponseStub() {
  const state: {
    statusCode?: number;
    jsonBody?: Record<string, unknown>;
    headers: Record<string, string>;
    ndjsonLines: string[];
  } = { headers: {}, ndjsonLines: [] };
  const res = {
    statusCode: undefined as number | undefined,
    jsonBody: undefined as Record<string, unknown> | undefined,
    status(code: number) {
      state.statusCode = code;
      this.statusCode = code;
      return this;
    },
    json(body: Record<string, unknown>) {
      state.jsonBody = body;
      this.jsonBody = body;
      return this;
    },
    setHeader(name: string, value: string) {
      state.headers[name] = value;
      return this;
    },
    write(chunk: string) {
      state.ndjsonLines.push(chunk);
      return true;
    },
    end() {},
    get state() {
      return state;
    },
  };
  return res;
}
