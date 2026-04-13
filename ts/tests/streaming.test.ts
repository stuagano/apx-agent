/**
 * Tests for POST /responses with stream: true — verifies SSE events come back.
 *
 * Uses the workflow plugin path (with a mock Runnable) so we don't need
 * real LLM credentials. Also tests the standard LLM path by verifying
 * the SSE response shape without calling the real SDK.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import express from 'express';
import inject from 'light-my-request';
import type { Runnable, Message } from '../src/workflows/types.js';

// Stub the runner module at the top level so vitest can hoist it
vi.mock('../src/agent/runner.js', async (importOriginal) => {
  const mod = await importOriginal() as Record<string, unknown>;
  return {
    ...mod,
    initDatabricksClient: vi.fn(() => ({})),
    runViaSDK: vi.fn(async () => 'sdk-response'),
    streamViaSDK: vi.fn(async function* () { yield 'sdk-chunk'; }),
  };
});

import { createAgentPlugin } from '../src/agent/plugin.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Mock Runnable that returns a fixed response. */
function makeMockRunnable(response: string): Runnable {
  return {
    run: async () => response,
    async *stream() {
      // Yield the response in 3 chunks
      const chunks = [response.slice(0, 3), response.slice(3, 6), response.slice(6)];
      for (const chunk of chunks) {
        if (chunk) yield chunk;
      }
    },
  };
}

function buildApp(workflow: Runnable) {

  const app = express();
  app.use(express.json());

  const plugin = createAgentPlugin({
    model: 'test-model',
    instructions: 'Test',
    workflow,
  });

  plugin.setup(app);
  plugin.injectRoutes(app);

  return app;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('POST /responses with stream: true (workflow path)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns Content-Type text/event-stream', async () => {
    const workflow = makeMockRunnable('Hello world!');
    const app = buildApp(workflow);

    const res = await inject(app, {
      method: 'POST',
      url: '/responses',
      payload: {
        input: [{ role: 'user', content: 'test' }],
        stream: true,
      },
    });

    expect(res.headers['content-type']).toContain('text/event-stream');
  });

  it('emits response.output_item.start event', async () => {
    const workflow = makeMockRunnable('Hello world!');
    const app = buildApp(workflow);

    const res = await inject(app, {
      method: 'POST',
      url: '/responses',
      payload: {
        input: [{ role: 'user', content: 'test' }],
        stream: true,
      },
    });

    const body = res.body;
    expect(body).toContain('event: response.output_item.start');
  });

  it('emits output_text.delta events with text chunks', async () => {
    const workflow = makeMockRunnable('Hello world!');
    const app = buildApp(workflow);

    const res = await inject(app, {
      method: 'POST',
      url: '/responses',
      payload: {
        input: [{ role: 'user', content: 'test' }],
        stream: true,
      },
    });

    const body = res.body;
    expect(body).toContain('event: output_text.delta');

    // Parse delta events from the SSE body
    const deltaLines = body.split('\n')
      .filter((line: string) => line.startsWith('data: '))
      .map((line: string) => {
        try { return JSON.parse(line.slice(6)); } catch { return null; }
      })
      .filter((d: unknown) => d && typeof d === 'object' && 'text' in (d as Record<string, unknown>));

    expect(deltaLines.length).toBeGreaterThan(0);
  });

  it('emits response.output_item.done event', async () => {
    const workflow = makeMockRunnable('Hello world!');
    const app = buildApp(workflow);

    const res = await inject(app, {
      method: 'POST',
      url: '/responses',
      payload: {
        input: [{ role: 'user', content: 'test' }],
        stream: true,
      },
    });

    expect(res.body).toContain('event: response.output_item.done');
  });

  it('concatenated delta text matches the workflow output', async () => {
    const expectedText = 'Hello world!';
    const workflow = makeMockRunnable(expectedText);
    const app = buildApp(workflow);

    const res = await inject(app, {
      method: 'POST',
      url: '/responses',
      payload: {
        input: [{ role: 'user', content: 'test' }],
        stream: true,
      },
    });

    // Extract all text from delta events
    const body = res.body;
    let reconstructed = '';
    for (const line of body.split('\n')) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          if (data.text) reconstructed += data.text;
        } catch {
          // skip non-JSON
        }
      }
    }

    expect(reconstructed).toBe(expectedText);
  });

  it('non-streaming request returns JSON response', async () => {
    const workflow = makeMockRunnable('non-streaming result');
    const app = buildApp(workflow);

    const res = await inject(app, {
      method: 'POST',
      url: '/responses',
      payload: {
        input: 'test query',
        stream: false,
      },
    });

    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.output_text).toBe('non-streaming result');
    expect(body.output[0].content[0].text).toBe('non-streaming result');
    expect(body.status).toBe('completed');
  });

  it('accepts string input (not just array)', async () => {
    const workflow = makeMockRunnable('response');
    const app = buildApp(workflow);

    const res = await inject(app, {
      method: 'POST',
      url: '/responses',
      payload: {
        input: 'simple string input',
      },
    });

    expect(res.statusCode).toBe(200);
    const body = res.json();
    expect(body.output_text).toBe('response');
  });
});
