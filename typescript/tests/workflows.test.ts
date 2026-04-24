/**
 * Tests for all workflow agents: Sequential, Parallel, Loop, Router, Handoff.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Message, Runnable } from '../src/workflows/types.js';
import { SequentialAgent } from '../src/workflows/sequential.js';
import { ParallelAgent } from '../src/workflows/parallel.js';
import { LoopAgent } from '../src/workflows/loop.js';
import { RouterAgent } from '../src/workflows/router.js';
import { HandoffAgent } from '../src/workflows/handoff.js';
import { AgentState } from '../src/workflows/state.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mockRunnable(response: string): Runnable {
  return { run: async () => response };
}

function mockRunnableFromFn(fn: (messages: Message[]) => Promise<string>): Runnable {
  return { run: fn };
}

function spyRunnable(response: string): Runnable & { calls: Message[][] } {
  const calls: Message[][] = [];
  return {
    calls,
    run: async (messages: Message[]) => {
      calls.push(messages);
      return response;
    },
  };
}

function makeMessages(...contents: string[]): Message[] {
  return contents.map((content) => ({ role: 'user', content }));
}

// ---------------------------------------------------------------------------
// SequentialAgent
// ---------------------------------------------------------------------------

describe('SequentialAgent', () => {
  it('throws when constructed with zero agents', () => {
    expect(() => new SequentialAgent([])).toThrow('SequentialAgent requires at least one agent');
  });

  it('runs a single agent and returns its result', async () => {
    const agent = new SequentialAgent([mockRunnable('hello')]);
    const result = await agent.run(makeMessages('hi'));
    expect(result).toBe('hello');
  });

  it('runs agents in order, passing context through', async () => {
    const order: number[] = [];
    const a1 = mockRunnableFromFn(async () => { order.push(1); return 'step1'; });
    const a2 = mockRunnableFromFn(async () => { order.push(2); return 'step2'; });
    const a3 = mockRunnableFromFn(async () => { order.push(3); return 'step3'; });

    const seq = new SequentialAgent([a1, a2, a3]);
    const result = await seq.run(makeMessages('go'));

    expect(order).toEqual([1, 2, 3]);
    expect(result).toBe('step3');
  });

  it('appends each agent output to context for the next agent', async () => {
    const capturedMessages: Message[][] = [];
    const a1 = mockRunnableFromFn(async (msgs) => { capturedMessages.push(msgs); return 'output-a1'; });
    const a2 = mockRunnableFromFn(async (msgs) => { capturedMessages.push(msgs); return 'output-a2'; });

    const seq = new SequentialAgent([a1, a2]);
    await seq.run([{ role: 'user', content: 'start' }]);

    // a2 should receive the original message plus a1's output
    const a2Messages = capturedMessages[1];
    expect(a2Messages.some((m) => m.content === 'output-a1')).toBe(true);
    expect(a2Messages.some((m) => m.role === 'assistant')).toBe(true);
  });

  it('returns the last agent result, not an intermediate one', async () => {
    const seq = new SequentialAgent([
      mockRunnable('first'),
      mockRunnable('second'),
      mockRunnable('final'),
    ]);
    const result = await seq.run(makeMessages('go'));
    expect(result).toBe('final');
  });

  it('prepends system instructions to context when provided', async () => {
    const captured: Message[][] = [];
    const agent = mockRunnableFromFn(async (msgs) => { captured.push(msgs); return 'ok'; });

    const seq = new SequentialAgent([agent], 'Be concise.');
    await seq.run(makeMessages('hello'));

    expect(captured[0][0]).toEqual({ role: 'system', content: 'Be concise.' });
  });

  it('does not prepend a system message when instructions are empty', async () => {
    const captured: Message[][] = [];
    const agent = mockRunnableFromFn(async (msgs) => { captured.push(msgs); return 'ok'; });

    const seq = new SequentialAgent([agent]);
    await seq.run(makeMessages('hello'));

    expect(captured[0].every((m) => m.role !== 'system')).toBe(true);
  });

  it('streams: runs all-but-last to completion, streams the last', async () => {
    const chunks: string[] = [];
    const last: Runnable = {
      run: async () => 'fallback',
      stream: async function* () { yield 'chunk1'; yield 'chunk2'; },
    };
    const seq = new SequentialAgent([mockRunnable('intermediate'), last]);

    for await (const chunk of seq.stream(makeMessages('go'))) {
      chunks.push(chunk);
    }

    expect(chunks).toEqual(['chunk1', 'chunk2']);
  });

  it('streams: falls back to run() when last agent has no stream method', async () => {
    const chunks: string[] = [];
    const seq = new SequentialAgent([mockRunnable('result')]);

    for await (const chunk of seq.stream(makeMessages('go'))) {
      chunks.push(chunk);
    }

    expect(chunks).toEqual(['result']);
  });

  it('collectTools returns merged tools from all agents', () => {
    const tool1 = { name: 't1', description: 'd1', parameters: {} as any, handler: async () => {} };
    const tool2 = { name: 't2', description: 'd2', parameters: {} as any, handler: async () => {} };
    const a1: Runnable = { run: async () => '', collectTools: () => [tool1] };
    const a2: Runnable = { run: async () => '', collectTools: () => [tool2] };

    const seq = new SequentialAgent([a1, a2]);
    expect(seq.collectTools()).toEqual([tool1, tool2]);
  });

  it('collectTools handles agents with no collectTools method', () => {
    const seq = new SequentialAgent([mockRunnable('ok')]);
    expect(seq.collectTools()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// ParallelAgent
// ---------------------------------------------------------------------------

describe('ParallelAgent', () => {
  it('throws when constructed with zero agents', () => {
    expect(() => new ParallelAgent([])).toThrow('ParallelAgent requires at least one agent');
  });

  it('runs all agents with the same messages', async () => {
    const captured: Message[][] = [];
    const a1 = mockRunnableFromFn(async (msgs) => { captured.push(msgs); return 'r1'; });
    const a2 = mockRunnableFromFn(async (msgs) => { captured.push(msgs); return 'r2'; });

    const par = new ParallelAgent([a1, a2]);
    const input = makeMessages('query');
    await par.run(input);

    expect(captured).toHaveLength(2);
    expect(captured[0]).toEqual(captured[1]);
  });

  it('joins results with double newline by default', async () => {
    const par = new ParallelAgent([mockRunnable('alpha'), mockRunnable('beta')]);
    const result = await par.run(makeMessages('go'));
    expect(result).toBe('alpha\n\nbeta');
  });

  it('uses custom separator when provided', async () => {
    const par = new ParallelAgent([mockRunnable('a'), mockRunnable('b')], { separator: ' | ' });
    const result = await par.run(makeMessages('go'));
    expect(result).toBe('a | b');
  });

  it('runs agents concurrently (all called before any resolves in practice)', async () => {
    const started: number[] = [];
    const makeDelayed = (id: number, ms: number) =>
      mockRunnableFromFn(async () => {
        started.push(id);
        await new Promise((r) => setTimeout(r, ms));
        return `r${id}`;
      });

    const par = new ParallelAgent([makeDelayed(1, 20), makeDelayed(2, 10)]);
    const result = await par.run(makeMessages('go'));

    // Both started (order may differ), result preserves agent order
    expect(started).toContain(1);
    expect(started).toContain(2);
    expect(result).toBe('r1\n\nr2');
  });

  it('prepends system instructions when provided', async () => {
    const captured: Message[][] = [];
    const agent = mockRunnableFromFn(async (msgs) => { captured.push(msgs); return 'ok'; });

    const par = new ParallelAgent([agent], { instructions: 'Be brief.' });
    await par.run(makeMessages('hello'));

    expect(captured[0][0]).toEqual({ role: 'system', content: 'Be brief.' });
  });

  it('stream yields the combined result once', async () => {
    const chunks: string[] = [];
    const par = new ParallelAgent([mockRunnable('x'), mockRunnable('y')]);

    for await (const chunk of par.stream(makeMessages('go'))) {
      chunks.push(chunk);
    }

    expect(chunks).toEqual(['x\n\ny']);
  });

  it('collectTools returns tools from all agents', () => {
    const tool = { name: 't', description: 'd', parameters: {} as any, handler: async () => {} };
    const a1: Runnable = { run: async () => '', collectTools: () => [tool] };
    const a2: Runnable = { run: async () => '' };

    const par = new ParallelAgent([a1, a2]);
    expect(par.collectTools()).toEqual([tool]);
  });
});

// ---------------------------------------------------------------------------
// LoopAgent
// ---------------------------------------------------------------------------

describe('LoopAgent', () => {
  it('runs the agent at least once with no stopWhen', async () => {
    const spy = spyRunnable('result');
    const loop = new LoopAgent(spy, { maxIterations: 3 });
    const result = await loop.run(makeMessages('go'));

    expect(spy.calls).toHaveLength(3);
    expect(result).toBe('result');
  });

  it('stops early when stopWhen returns true', async () => {
    let count = 0;
    const agent = mockRunnableFromFn(async () => `iteration-${++count}`);
    const loop = new LoopAgent(agent, {
      maxIterations: 10,
      stopWhen: (result) => result.includes('2'),
    });

    const result = await loop.run(makeMessages('go'));
    expect(result).toBe('iteration-2');
    expect(count).toBe(2);
  });

  it('respects maxIterations even without stopWhen', async () => {
    let count = 0;
    const agent = mockRunnableFromFn(async () => { count++; return 'continue'; });
    const loop = new LoopAgent(agent, { maxIterations: 4 });
    await loop.run(makeMessages('go'));
    expect(count).toBe(4);
  });

  it('defaults to maxIterations 5', async () => {
    let count = 0;
    const agent = mockRunnableFromFn(async () => { count++; return 'x'; });
    const loop = new LoopAgent(agent);
    await loop.run(makeMessages('go'));
    expect(count).toBe(5);
  });

  it('passes iteration index to stopWhen predicate', async () => {
    const indices: number[] = [];
    const agent = mockRunnableFromFn(async () => 'ok');
    const loop = new LoopAgent(agent, {
      maxIterations: 5,
      stopWhen: (_result, i) => { indices.push(i); return i >= 2; },
    });

    await loop.run(makeMessages('go'));
    // stopWhen is called at i=0, i=1, i=2 (stops at i=2)
    expect(indices).toEqual([0, 1, 2]);
  });

  it('appends previous result to context each iteration', async () => {
    const contexts: Message[][] = [];
    const agent = mockRunnableFromFn(async (msgs) => {
      contexts.push(msgs);
      return `step-${contexts.length}`;
    });
    const loop = new LoopAgent(agent, {
      maxIterations: 3,
      stopWhen: () => false,
    });

    await loop.run([{ role: 'user', content: 'start' }]);

    // First iteration: just the original message
    expect(contexts[0]).toHaveLength(1);
    // Second iteration: original + first result
    expect(contexts[1]).toHaveLength(2);
    expect(contexts[1][1]).toEqual({ role: 'assistant', content: 'step-1' });
  });

  it('stream yields the final result once', async () => {
    const chunks: string[] = [];
    const loop = new LoopAgent(mockRunnable('done'), { maxIterations: 2 });

    for await (const chunk of loop.stream(makeMessages('go'))) {
      chunks.push(chunk);
    }

    expect(chunks).toHaveLength(1);
    expect(chunks[0]).toBe('done');
  });

  it('collectTools delegates to inner agent', () => {
    const tool = { name: 't', description: 'd', parameters: {} as any, handler: async () => {} };
    const agent: Runnable = { run: async () => '', collectTools: () => [tool] };
    const loop = new LoopAgent(agent);
    expect(loop.collectTools()).toEqual([tool]);
  });

  it('collectTools returns empty when inner agent has no method', () => {
    const loop = new LoopAgent(mockRunnable('x'));
    expect(loop.collectTools()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// RouterAgent
// ---------------------------------------------------------------------------

describe('RouterAgent', () => {
  it('throws when constructed with zero routes', () => {
    expect(() => new RouterAgent({ routes: [] })).toThrow('RouterAgent requires at least one route');
  });

  it('routes to the first matching condition', async () => {
    const billing = spyRunnable('billing response');
    const support = spyRunnable('support response');

    const router = new RouterAgent({
      routes: [
        { name: 'billing', description: 'Billing', agent: billing, condition: (msgs) => msgs.some((m) => m.content.includes('bill')) },
        { name: 'support', description: 'Support', agent: support, condition: (msgs) => msgs.some((m) => m.content.includes('support')) },
      ],
    });

    const result = await router.run([{ role: 'user', content: 'I have a bill question' }]);
    expect(result).toBe('billing response');
    expect(billing.calls).toHaveLength(1);
    expect(support.calls).toHaveLength(0);
  });

  it('skips conditions that do not match', async () => {
    const a = spyRunnable('A');
    const b = spyRunnable('B');

    const router = new RouterAgent({
      routes: [
        { name: 'a', description: 'A', agent: a, condition: (msgs) => msgs.some((m) => m.content.includes('AAA')) },
        { name: 'b', description: 'B', agent: b, condition: (msgs) => msgs.some((m) => m.content.includes('bbb')) },
      ],
      fallback: mockRunnable('fallback'),
    });

    const result = await router.run([{ role: 'user', content: 'unrelated' }]);
    expect(result).toBe('fallback');
    expect(a.calls).toHaveLength(0);
    expect(b.calls).toHaveLength(0);
  });

  it('uses fallback agent when no condition matches', async () => {
    const fallback = spyRunnable('default answer');
    const router = new RouterAgent({
      routes: [
        { name: 'billing', description: 'Billing', agent: mockRunnable('billing'), condition: () => false },
      ],
      fallback,
    });

    const result = await router.run(makeMessages('anything'));
    expect(result).toBe('default answer');
    expect(fallback.calls).toHaveLength(1);
  });

  it('falls back to first route when no fallback configured and no condition matches', async () => {
    const first = spyRunnable('first agent');
    const second = spyRunnable('second agent');

    const router = new RouterAgent({
      routes: [
        { name: 'first', description: 'First', agent: first },
        { name: 'second', description: 'Second', agent: second },
      ],
    });

    const result = await router.run(makeMessages('anything'));
    expect(result).toBe('first agent');
    expect(second.calls).toHaveLength(0);
  });

  it('passes messages to the selected route unchanged', async () => {
    const captured: Message[][] = [];
    const agent = mockRunnableFromFn(async (msgs) => { captured.push(msgs); return 'ok'; });

    const router = new RouterAgent({
      routes: [{ name: 'r', description: 'R', agent, condition: () => true }],
    });

    const input: Message[] = [{ role: 'user', content: 'my question' }];
    await router.run(input);
    expect(captured[0]).toEqual(input);
  });

  it('evaluates conditions in order and picks the first match', async () => {
    const first = spyRunnable('first match');
    const second = spyRunnable('second match');

    const router = new RouterAgent({
      routes: [
        { name: 'first', description: 'First', agent: first, condition: () => true },
        { name: 'second', description: 'Second', agent: second, condition: () => true },
      ],
    });

    const result = await router.run(makeMessages('go'));
    expect(result).toBe('first match');
    expect(second.calls).toHaveLength(0);
  });

  it('stream delegates to selected route stream method', async () => {
    const chunks: string[] = [];
    const streamable: Runnable = {
      run: async () => 'fallback',
      stream: async function* () { yield 'streamed'; },
    };

    const router = new RouterAgent({
      routes: [{ name: 'r', description: 'R', agent: streamable, condition: () => true }],
    });

    for await (const chunk of router.stream(makeMessages('go'))) {
      chunks.push(chunk);
    }

    expect(chunks).toEqual(['streamed']);
  });

  it('stream falls back to run() when route has no stream method', async () => {
    const chunks: string[] = [];
    const router = new RouterAgent({
      routes: [{ name: 'r', description: 'R', agent: mockRunnable('result'), condition: () => true }],
    });

    for await (const chunk of router.stream(makeMessages('go'))) {
      chunks.push(chunk);
    }

    expect(chunks).toEqual(['result']);
  });

  it('collectTools returns tools from all routes', () => {
    const tool1 = { name: 't1', description: 'd1', parameters: {} as any, handler: async () => {} };
    const tool2 = { name: 't2', description: 'd2', parameters: {} as any, handler: async () => {} };
    const a1: Runnable = { run: async () => '', collectTools: () => [tool1] };
    const a2: Runnable = { run: async () => '', collectTools: () => [tool2] };

    const router = new RouterAgent({
      routes: [
        { name: 'r1', description: 'R1', agent: a1 },
        { name: 'r2', description: 'R2', agent: a2 },
      ],
    });

    expect(router.collectTools()).toEqual([tool1, tool2]);
  });

  // -------------------------------------------------------------------------
  // LLM-based routing
  // -------------------------------------------------------------------------

  describe('LLM-based routing', () => {
    const originalFetch = globalThis.fetch;
    const originalEnv = process.env.DATABRICKS_HOST;

    beforeEach(() => {
      process.env.DATABRICKS_HOST = 'https://test.cloud.databricks.com';
      process.env.DATABRICKS_TOKEN = 'test-token';
    });

    afterEach(() => {
      globalThis.fetch = originalFetch;
      if (originalEnv !== undefined) {
        process.env.DATABRICKS_HOST = originalEnv;
      } else {
        delete process.env.DATABRICKS_HOST;
      }
    });

    function mockFetchForRoute(routeName: string) {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          choices: [{
            message: {
              tool_calls: [{
                id: 'call_1',
                type: 'function',
                function: {
                  name: 'select_route',
                  arguments: JSON.stringify({ route_name: routeName }),
                },
              }],
            },
          }],
        }),
      }) as any;
    }

    it('uses LLM to select route when model + instructions set and no condition matches', async () => {
      const billing = spyRunnable('billing response');
      const support = spyRunnable('support response');

      mockFetchForRoute('support');

      const router = new RouterAgent({
        model: 'databricks-claude-sonnet-4-6',
        instructions: 'Route to the appropriate agent.',
        routes: [
          { name: 'billing', description: 'Billing inquiries', agent: billing },
          { name: 'support', description: 'Technical support', agent: support },
        ],
      });

      const result = await router.run(makeMessages('my app is crashing'));
      expect(result).toBe('support response');
      expect(support.calls).toHaveLength(1);
      expect(billing.calls).toHaveLength(0);
    });

    it('deterministic conditions take priority over LLM routing', async () => {
      const billing = spyRunnable('billing response');
      const support = spyRunnable('support response');

      // LLM would pick support, but deterministic condition matches billing
      mockFetchForRoute('support');

      const router = new RouterAgent({
        model: 'databricks-claude-sonnet-4-6',
        instructions: 'Route to the appropriate agent.',
        routes: [
          { name: 'billing', description: 'Billing', agent: billing,
            condition: (msgs) => msgs.some((m) => m.content.includes('bill')) },
          { name: 'support', description: 'Support', agent: support },
        ],
      });

      const result = await router.run(makeMessages('I have a bill question'));
      expect(result).toBe('billing response');
      // fetch should NOT have been called — deterministic matched first
      expect(globalThis.fetch).not.toHaveBeenCalled();
    });

    it('falls back to fallback agent when LLM returns unknown route name', async () => {
      const fallback = spyRunnable('fallback response');
      mockFetchForRoute('nonexistent_route');

      const router = new RouterAgent({
        model: 'databricks-claude-sonnet-4-6',
        instructions: 'Route appropriately.',
        routes: [
          { name: 'billing', description: 'Billing', agent: mockRunnable('billing') },
        ],
        fallback,
      });

      const result = await router.run(makeMessages('anything'));
      expect(result).toBe('fallback response');
    });

    it('falls back when LLM call fails with network error', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('Network error'));

      const fallback = spyRunnable('fallback response');
      const router = new RouterAgent({
        model: 'databricks-claude-sonnet-4-6',
        instructions: 'Route appropriately.',
        routes: [
          { name: 'billing', description: 'Billing', agent: mockRunnable('billing') },
        ],
        fallback,
      });

      const result = await router.run(makeMessages('anything'));
      expect(result).toBe('fallback response');
    });

    it('falls back when LLM returns non-ok status', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        text: async () => 'Internal server error',
      }) as any;

      const first = spyRunnable('first route');
      const router = new RouterAgent({
        model: 'databricks-claude-sonnet-4-6',
        instructions: 'Route appropriately.',
        routes: [
          { name: 'first', description: 'First', agent: first },
        ],
      });

      const result = await router.run(makeMessages('anything'));
      expect(result).toBe('first route');
    });

    it('does not attempt LLM routing when model is not set', async () => {
      const fetchSpy = vi.fn();
      globalThis.fetch = fetchSpy;

      const first = spyRunnable('first route');
      const router = new RouterAgent({
        instructions: 'Route appropriately.',
        routes: [
          { name: 'first', description: 'First', agent: first },
        ],
      });

      await router.run(makeMessages('anything'));
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('sends route names as enum in tool definition', async () => {
      const fetchSpy = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          choices: [{
            message: {
              tool_calls: [{
                id: 'call_1',
                type: 'function',
                function: {
                  name: 'select_route',
                  arguments: JSON.stringify({ route_name: 'alpha' }),
                },
              }],
            },
          }],
        }),
      });
      globalThis.fetch = fetchSpy;

      const router = new RouterAgent({
        model: 'test-model',
        instructions: 'Pick one.',
        routes: [
          { name: 'alpha', description: 'Alpha agent', agent: mockRunnable('a') },
          { name: 'beta', description: 'Beta agent', agent: mockRunnable('b') },
        ],
      });

      await router.run(makeMessages('go'));

      const body = JSON.parse(fetchSpy.mock.calls[0][1].body);
      const toolParams = body.tools[0].function.parameters;
      expect(toolParams.properties.route_name.enum).toEqual(['alpha', 'beta']);
      expect(body.tool_choice).toEqual({ type: 'function', function: { name: 'select_route' } });
    });
  });
});

// ---------------------------------------------------------------------------
// HandoffAgent
// ---------------------------------------------------------------------------

describe('HandoffAgent', () => {
  it('throws when start agent is not in agents map', () => {
    expect(() => new HandoffAgent({
      agents: { triage: mockRunnable('ok') },
      start: 'missing',
    })).toThrow("HandoffAgent start='missing' not found in agents");
  });

  it('runs the start agent and returns its response when no handoff occurs', async () => {
    const system = new HandoffAgent({
      agents: { triage: mockRunnable('triage response') },
      start: 'triage',
    });

    const result = await system.run(makeMessages('hello'));
    expect(result).toBe('triage response');
  });

  it('detects TRANSFER pattern and switches to the target agent', async () => {
    const triage = mockRunnable('TRANSFER: billing');
    const billing = mockRunnable('billing answer');

    const system = new HandoffAgent({
      agents: { triage, billing },
      start: 'triage',
      maxHandoffs: 3,
    });

    const result = await system.run(makeMessages('I need billing help'));
    expect(result).toBe('billing answer');
  });

  it('is case-insensitive for TRANSFER keyword', async () => {
    const triage = mockRunnable('transfer: support');
    const support = mockRunnable('support answer');

    const system = new HandoffAgent({
      agents: { triage, support },
      start: 'triage',
    });

    const result = await system.run(makeMessages('need support'));
    expect(result).toBe('support answer');
  });

  it('stops after target agent responds with no further transfer', async () => {
    const a = mockRunnable('TRANSFER: b');
    const b = mockRunnable('final answer');

    const system = new HandoffAgent({
      agents: { a, b },
      start: 'a',
    });

    const result = await system.run(makeMessages('go'));
    expect(result).toBe('final answer');
  });

  it('respects maxHandoffs limit', async () => {
    let callCount = 0;
    const bouncer: Runnable = {
      run: async () => {
        callCount++;
        return 'TRANSFER: other';
      },
    };
    const other: Runnable = {
      run: async () => {
        callCount++;
        return 'TRANSFER: bouncer';
      },
    };

    const system = new HandoffAgent({
      agents: { bouncer, other },
      start: 'bouncer',
      maxHandoffs: 2,
    });

    await system.run(makeMessages('go'));
    // maxHandoffs=2 means the loop runs at most maxHandoffs+1=3 times
    expect(callCount).toBeLessThanOrEqual(3);
  });

  it('calls onHandoff callback when a transfer occurs', async () => {
    const handoffs: Array<{ from: string; to: string }> = [];
    const triage = mockRunnable('TRANSFER: billing');
    const billing = mockRunnable('done');

    const system = new HandoffAgent({
      agents: { triage, billing },
      start: 'triage',
      onHandoff: (from, to) => handoffs.push({ from, to }),
    });

    await system.run(makeMessages('go'));
    expect(handoffs).toEqual([{ from: 'triage', to: 'billing' }]);
  });

  it('does not transfer to an unknown agent name', async () => {
    const triage = mockRunnable('TRANSFER: nonexistent');

    const system = new HandoffAgent({
      agents: { triage },
      start: 'triage',
    });

    // Should return the triage response (no valid handoff target)
    const result = await system.run(makeMessages('go'));
    expect(result).toBe('TRANSFER: nonexistent');
  });

  it('does not transfer to self', async () => {
    let count = 0;
    const agent: Runnable = {
      run: async () => {
        count++;
        return count === 1 ? 'TRANSFER: self' : 'no more transfers';
      },
    };

    const system = new HandoffAgent({
      agents: { self: agent },
      start: 'self',
      maxHandoffs: 3,
    });

    const result = await system.run(makeMessages('go'));
    // Self-transfer is not allowed, so it breaks out with the first response
    expect(result).toBe('TRANSFER: self');
    expect(count).toBe(1);
  });

  it('appends handoff context to conversation history', async () => {
    const captured: Message[][] = [];
    const triage = mockRunnable('TRANSFER: billing');
    const billing: Runnable = {
      run: async (msgs) => {
        captured.push(msgs);
        return 'billing done';
      },
    };

    const system = new HandoffAgent({
      agents: { triage, billing },
      start: 'triage',
    });

    await system.run([{ role: 'user', content: 'billing question' }]);

    // billing should receive the original message + triage's output + handoff system message
    expect(captured[0].some((m) => m.content.includes('TRANSFER: billing'))).toBe(true);
    expect(captured[0].some((m) => m.content.includes('Handed off from triage to billing'))).toBe(true);
  });

  it('stream yields the final result once', async () => {
    const chunks: string[] = [];
    const system = new HandoffAgent({
      agents: { start: mockRunnable('response') },
      start: 'start',
    });

    for await (const chunk of system.stream(makeMessages('go'))) {
      chunks.push(chunk);
    }

    expect(chunks).toHaveLength(1);
    expect(chunks[0]).toBe('response');
  });

  it('collectTools returns tools from all agents in the map', () => {
    const tool1 = { name: 't1', description: 'd1', parameters: {} as any, handler: async () => {} };
    const tool2 = { name: 't2', description: 'd2', parameters: {} as any, handler: async () => {} };
    const a1: Runnable = { run: async () => '', collectTools: () => [tool1] };
    const a2: Runnable = { run: async () => '', collectTools: () => [tool2] };

    const system = new HandoffAgent({
      agents: { a: a1, b: a2 },
      start: 'a',
    });

    expect(system.collectTools()).toEqual([tool1, tool2]);
  });

  it('defaults maxHandoffs to 5', async () => {
    let callCount = 0;
    // Agent that always tries to transfer to "other"
    const makeAgent = (target: string): Runnable => ({
      run: async () => {
        callCount++;
        return `TRANSFER: ${target}`;
      },
    });

    const system = new HandoffAgent({
      agents: { a: makeAgent('b'), b: makeAgent('a') },
      start: 'a',
      // no maxHandoffs specified — should default to 5
    });

    await system.run(makeMessages('go'));
    // Loop runs maxHandoffs+1 = 6 times at most
    expect(callCount).toBeLessThanOrEqual(6);
  });
});

// ---------------------------------------------------------------------------
// AgentState (bonus — it lives in workflows/)
// ---------------------------------------------------------------------------

describe('AgentState', () => {
  it('initializes with optional seed values', () => {
    const state = new AgentState({ topic: 'billing', count: 3 });
    expect(state.get('topic')).toBe('billing');
    expect(state.get('count')).toBe(3);
  });

  it('get returns undefined for unknown keys', () => {
    const state = new AgentState();
    expect(state.get('nope')).toBeUndefined();
  });

  it('set and get round-trip', () => {
    const state = new AgentState();
    state.set('key', 'value');
    expect(state.get('key')).toBe('value');
  });

  it('has returns true for existing keys and false otherwise', () => {
    const state = new AgentState({ x: 1 });
    expect(state.has('x')).toBe(true);
    expect(state.has('y')).toBe(false);
  });

  it('delete removes a key', () => {
    const state = new AgentState({ a: 1 });
    state.delete('a');
    expect(state.has('a')).toBe(false);
  });

  it('keys returns all keys', () => {
    const state = new AgentState({ a: 1, b: 2 });
    expect(state.keys().sort()).toEqual(['a', 'b']);
  });

  it('toObject returns plain object snapshot', () => {
    const state = new AgentState({ x: 1, y: 'two' });
    expect(state.toObject()).toEqual({ x: 1, y: 'two' });
  });

  it('clearTemp removes keys with temp: prefix', () => {
    const state = new AgentState();
    state.set('persist', 'stay');
    state.set('temp:scratch', 'gone');
    state.set('temp:work', 'also gone');
    state.clearTemp();

    expect(state.has('persist')).toBe(true);
    expect(state.has('temp:scratch')).toBe(false);
    expect(state.has('temp:work')).toBe(false);
  });

  it('interpolate replaces known placeholders', () => {
    const state = new AgentState({ topic: 'billing', user: 'Alice' });
    const result = state.interpolate('Handle {topic} for {user}.');
    expect(result).toBe('Handle billing for Alice.');
  });

  it('interpolate leaves unknown placeholders intact', () => {
    const state = new AgentState({ known: 'yes' });
    const result = state.interpolate('Hello {known} and {unknown}');
    expect(result).toBe('Hello yes and {unknown}');
  });

  it('clone creates an independent copy', () => {
    const state = new AgentState({ a: 1 });
    const copy = state.clone();
    copy.set('a', 99);
    expect(state.get('a')).toBe(1);
  });
});
