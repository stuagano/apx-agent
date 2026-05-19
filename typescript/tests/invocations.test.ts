/**
 * Tests for invocations.ts — POST /invocations route for the MLflow
 * ChatAgent wire protocol.
 *
 * Mirrors python/tests/test_invocations_route.py to the extent the TS
 * version supports (NDJSON streaming instead of SSE).
 *
 * Uses a real Express app booted on an ephemeral port and Node's fetch
 * to exercise the route — avoids needing supertest / light-my-request.
 */

import { describe, it, expect, beforeAll, afterAll, beforeEach, vi } from 'vitest';
import express from 'express';
import type { AddressInfo } from 'node:net';
import { createServer, type Server } from 'node:http';
import {
  mountInvocationsRoute,
  type ChatAgentChunk,
  type ChatAgentLike,
  type ChatAgentMessage,
  type ChatAgentResponse,
} from '../src/invocations.js';

// ---------------------------------------------------------------------------
// Test harness — ephemeral HTTP server with the route mounted
// ---------------------------------------------------------------------------

interface Harness {
  baseUrl: string;
  server: Server;
  chatAgent: ChatAgentLike & {
    predict: ReturnType<typeof vi.fn>;
    predictStream: ReturnType<typeof vi.fn>;
  };
  close: () => Promise<void>;
}

async function buildHarness(opts: { withStream?: boolean } = {}): Promise<Harness> {
  const app = express();
  app.use(express.json());

  const predict = vi.fn(
    async (
      _messages: ChatAgentMessage[],
      _opts?: { customInputs?: Record<string, unknown> },
    ): Promise<ChatAgentResponse> => ({
      messages: [{ role: 'assistant', content: 'default-response', id: 'm1' }],
    }),
  );
  // Default predictStream yields two trivial chunks.
  const predictStream = vi.fn(function* (
    _messages: ChatAgentMessage[],
    _opts?: { customInputs?: Record<string, unknown> },
  ): IterableIterator<ChatAgentChunk> {
    yield { delta: { role: 'assistant', content: 'hel', id: 'm1' } };
    yield { delta: { role: 'assistant', content: 'lo', id: 'm1' } };
  });

  const chatAgent: Harness['chatAgent'] = {
    predict: predict as never,
    predictStream: predictStream as never,
  };

  const mounted = mountInvocationsRoute({
    app,
    chatAgent: opts.withStream === false ? { predict } as never : chatAgent,
  });
  expect(mounted).toBe(true);

  const server = createServer(app);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const addr = server.address() as AddressInfo;
  const baseUrl = `http://127.0.0.1:${addr.port}`;

  return {
    baseUrl,
    server,
    chatAgent,
    close: () =>
      new Promise<void>((resolve, reject) =>
        server.close((err) => (err ? reject(err) : resolve())),
      ),
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('mountInvocationsRoute — registration', () => {
  let h: Harness;

  beforeAll(async () => {
    h = await buildHarness();
  });
  afterAll(async () => {
    await h.close();
  });

  it('registers POST /invocations', async () => {
    const res = await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }] }),
    });
    expect(res.status).toBe(200);
  });

  it('also registers POST /api/invocations (the mirror)', async () => {
    const res = await fetch(`${h.baseUrl}/api/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }] }),
    });
    expect(res.status).toBe(200);
  });

  it('returns false when chatAgent lacks predict', () => {
    const app = express();
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const ok = mountInvocationsRoute({
      app,
      chatAgent: {} as ChatAgentLike,
    });
    expect(ok).toBe(false);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe('mountInvocationsRoute — non-streaming protocol', () => {
  let h: Harness;

  beforeAll(async () => {
    h = await buildHarness();
  });
  afterAll(async () => {
    await h.close();
  });
  beforeEach(() => {
    h.chatAgent.predict.mockClear();
    h.chatAgent.predictStream.mockClear();
  });

  it('forwards messages, context, and customInputs into predict', async () => {
    h.chatAgent.predict.mockResolvedValue({
      messages: [{ role: 'assistant', content: 'echo', id: 'm1' }],
    });

    const res = await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'hi' }],
        context: { conversation_id: 'c1', user_id: 'u1' },
        customInputs: { extra: 'value' },
      }),
    });

    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.messages[0].content).toBe('echo');

    expect(h.chatAgent.predict).toHaveBeenCalledTimes(1);
    const [messages, opts] = h.chatAgent.predict.mock.calls[0] as [
      ChatAgentMessage[],
      { context?: unknown; customInputs?: Record<string, unknown> } | undefined,
    ];
    expect(messages).toEqual([{ role: 'user', content: 'hi' }]);
    expect(opts?.context).toEqual({ conversation_id: 'c1', user_id: 'u1' });
    expect(opts?.customInputs).toEqual({ extra: 'value' });
  });

  it('accepts snake_case custom_inputs for Python-protocol parity', async () => {
    h.chatAgent.predict.mockResolvedValue({ messages: [] });

    await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'hi' }],
        custom_inputs: { from_snake: true },
      }),
    });

    const opts = h.chatAgent.predict.mock.calls[0]?.[1] as {
      customInputs?: Record<string, unknown>;
    };
    expect(opts?.customInputs).toEqual({ from_snake: true });
  });

  it('OBO header → customInputs.user_token', async () => {
    h.chatAgent.predict.mockResolvedValue({ messages: [] });

    await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Forwarded-Access-Token': 'secret-obo-tok',
      },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }] }),
    });

    const opts = h.chatAgent.predict.mock.calls[0]?.[1] as {
      customInputs?: Record<string, unknown>;
    };
    expect(opts?.customInputs?.user_token).toBe('secret-obo-tok');
  });

  it('caller-provided user_token wins over the OBO header', async () => {
    h.chatAgent.predict.mockResolvedValue({ messages: [] });

    await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Forwarded-Access-Token': 'header-tok',
      },
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'hi' }],
        customInputs: { user_token: 'explicit-tok' },
      }),
    });

    const opts = h.chatAgent.predict.mock.calls[0]?.[1] as {
      customInputs?: Record<string, unknown>;
    };
    expect(opts?.customInputs?.user_token).toBe('explicit-tok');
  });
});

describe('mountInvocationsRoute — streaming protocol (NDJSON)', () => {
  let h: Harness;

  beforeAll(async () => {
    h = await buildHarness();
  });
  afterAll(async () => {
    await h.close();
  });
  beforeEach(() => {
    h.chatAgent.predict.mockClear();
    h.chatAgent.predictStream.mockClear();
  });

  it('streams NDJSON when customInputs.stream === true', async () => {
    const res = await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'hi' }],
        customInputs: { stream: true },
      }),
    });

    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('application/x-ndjson');
    const text = await res.text();
    const lines = text.split('\n').filter((l) => l.length > 0);
    expect(lines.length).toBe(2);
    const first = JSON.parse(lines[0]);
    const second = JSON.parse(lines[1]);
    expect(first.delta.content).toBe('hel');
    expect(second.delta.content).toBe('lo');
  });

  it('does not pass the stream meta-flag into the agent customInputs', async () => {
    await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: [{ role: 'user', content: 'hi' }],
        customInputs: { stream: true, keep: 'me' },
      }),
    });

    const opts = h.chatAgent.predictStream.mock.calls[0]?.[1] as {
      customInputs?: Record<string, unknown>;
    };
    expect(opts?.customInputs).toEqual({ keep: 'me' });
    expect(opts?.customInputs?.stream).toBeUndefined();
  });
});

describe('mountInvocationsRoute — error handling', () => {
  let h: Harness;

  beforeAll(async () => {
    h = await buildHarness();
  });
  afterAll(async () => {
    await h.close();
  });
  beforeEach(() => {
    h.chatAgent.predict.mockClear();
  });

  it('returns 400 when body lacks messages', async () => {
    const res = await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ context: { conversation_id: 'c1' } }),
    });
    expect(res.status).toBe(400);
    const body = await res.json();
    expect(body.error).toMatch(/messages/);
  });

  it('returns 400 when a message lacks a role', async () => {
    const res = await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ content: 'orphan' }] }),
    });
    expect(res.status).toBe(400);
  });

  it('returns 500 with a sanitized message when predict throws', async () => {
    h.chatAgent.predict.mockRejectedValueOnce(
      new Error('SECRET: stack trace that should not leak'),
    );
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    const res = await fetch(`${h.baseUrl}/invocations`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }] }),
    });

    expect(res.status).toBe(500);
    const body = await res.json();
    expect(body.error).toBe('Internal error processing invocation.');
    expect(body.error).not.toMatch(/SECRET/);
    expect(body.error).not.toMatch(/stack/);

    errSpy.mockRestore();
  });
});
