/**
 * Tests for runOnce() — the batch/scheduled-job invocation primitive.
 *
 * Mirrors python/tests covering the run_once module: prompt validation,
 * model resolution, runViaSDK delegation, and final-text extraction.
 *
 * The injected `runViaSDK` mock represents the compiled-chat-agent
 * predict() in Python — it already returns the final assistant text
 * as a string, so tests assert on the return value directly.
 */

import { describe, it, expect, vi } from 'vitest';
import { z } from 'zod';
import { runOnce, type RunViaSDKFn } from '../src/run-once.js';
import type { AgentConfig, AgentExports } from '../src/agent/plugin.js';
import type { AgentTool } from '../src/agent/tools.js';

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

function makeExports(config: AgentConfig, tools: AgentTool[] = []): AgentExports {
  return {
    getConfig: () => config,
    getTools: () => tools,
    getToolSchemas: () => [],
  };
}

function makeRunner(returnValue = 'final assistant text'): RunViaSDKFn {
  return vi.fn(async () => returnValue) as unknown as RunViaSDKFn;
}

// ---------------------------------------------------------------------------
// Prompt validation
// ---------------------------------------------------------------------------

describe('runOnce — prompt validation', () => {
  it('throws ZodError when prompt is empty string', async () => {
    await expect(
      runOnce({
        agent: makeConfig(),
        prompt: '',
        runViaSDK: makeRunner(),
      }),
    ).rejects.toBeInstanceOf(z.ZodError);
  });

  it('throws ZodError when prompt is missing (undefined)', async () => {
    await expect(
      runOnce({
        agent: makeConfig(),
        // @ts-expect-error — intentionally invalid for this test
        prompt: undefined,
        runViaSDK: makeRunner(),
      }),
    ).rejects.toBeInstanceOf(z.ZodError);
  });

  it('throws ZodError when prompt is not a string', async () => {
    await expect(
      runOnce({
        agent: makeConfig(),
        // @ts-expect-error — intentionally invalid for this test
        prompt: 42,
        runViaSDK: makeRunner(),
      }),
    ).rejects.toBeInstanceOf(z.ZodError);
  });
});

// ---------------------------------------------------------------------------
// Model resolution
// ---------------------------------------------------------------------------

describe('runOnce — model resolution', () => {
  it('uses explicit opts.model when provided', async () => {
    const runner = makeRunner();
    await runOnce({
      agent: makeConfig({ model: 'agent-default-model' }),
      prompt: 'hi',
      model: 'explicit-model',
      runViaSDK: runner,
    });
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.model).toBe('explicit-model');
  });

  it('falls back to agent config model when opts.model is omitted', async () => {
    const runner = makeRunner();
    await runOnce({
      agent: makeConfig({ model: 'agent-default-model' }),
      prompt: 'hi',
      runViaSDK: runner,
    });
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.model).toBe('agent-default-model');
  });

  it('resolves model from AgentExports.getConfig()', async () => {
    const runner = makeRunner();
    const exports = makeExports(makeConfig({ model: 'exported-model' }));
    await runOnce({
      agent: exports,
      prompt: 'hi',
      runViaSDK: runner,
    });
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.model).toBe('exported-model');
  });

  it('throws Error when no model is resolvable from agent or opts', async () => {
    const agentWithoutModel = { name: 'no-model' };
    await expect(
      runOnce({
        agent: agentWithoutModel,
        prompt: 'hi',
        runViaSDK: makeRunner(),
      }),
    ).rejects.toThrow(/requires a model/i);
  });

  it('throws Error when agent is an unrecognized shape and no model provided', async () => {
    await expect(
      runOnce({
        agent: null,
        prompt: 'hi',
        runViaSDK: makeRunner(),
      }),
    ).rejects.toThrow(/requires a model/i);
  });
});

// ---------------------------------------------------------------------------
// runViaSDK delegation
// ---------------------------------------------------------------------------

describe('runOnce — runViaSDK delegation', () => {
  it('calls runViaSDK with messages=[{role:user, content:prompt}]', async () => {
    const runner = makeRunner();
    await runOnce({
      agent: makeConfig(),
      prompt: 'do the thing',
      runViaSDK: runner,
    });

    expect(runner).toHaveBeenCalledTimes(1);
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.messages).toEqual([{ role: 'user', content: 'do the thing' }]);
  });

  it('passes resolved model to runViaSDK', async () => {
    const runner = makeRunner();
    await runOnce({
      agent: makeConfig({ model: 'm1' }),
      prompt: 'hi',
      model: 'm-override',
      runViaSDK: runner,
    });
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.model).toBe('m-override');
  });

  it('forwards instructions from agent config', async () => {
    const runner = makeRunner();
    await runOnce({
      agent: makeConfig({ instructions: 'be concise' }),
      prompt: 'hi',
      runViaSDK: runner,
    });
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.instructions).toBe('be concise');
  });

  it('defaults instructions to empty string when agent has none', async () => {
    const runner = makeRunner();
    const cfg = makeConfig();
    delete cfg.instructions;
    await runOnce({
      agent: cfg,
      prompt: 'hi',
      runViaSDK: runner,
    });
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.instructions).toBe('');
  });

  it('forwards tools from AgentExports.getTools() when given exports', async () => {
    const runner = makeRunner();
    const fakeTools = [
      { name: 't1', description: 'd', parameters: z.object({}), handler: async () => '' },
    ] as AgentTool[];
    const exports = makeExports(makeConfig(), fakeTools);
    await runOnce({
      agent: exports,
      prompt: 'hi',
      runViaSDK: runner,
    });
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.tools).toBe(fakeTools);
  });

  it('forwards tools from AgentConfig.tools when given a raw config', async () => {
    const runner = makeRunner();
    const fakeTools = [
      { name: 't2', description: 'd', parameters: z.object({}), handler: async () => '' },
    ] as AgentTool[];
    await runOnce({
      agent: makeConfig({ tools: fakeTools }),
      prompt: 'hi',
      runViaSDK: runner,
    });
    const args = (runner as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.tools).toBe(fakeTools);
  });
});

// ---------------------------------------------------------------------------
// Return value
// ---------------------------------------------------------------------------

describe('runOnce — return value', () => {
  it('returns the final assistant text from runViaSDK', async () => {
    const runner = makeRunner('the actual final answer');
    const result = await runOnce({
      agent: makeConfig(),
      prompt: 'hi',
      runViaSDK: runner,
    });
    expect(result).toBe('the actual final answer');
  });

  it('returns empty string when runViaSDK returns empty (no assistant message)', async () => {
    const runner = makeRunner('');
    const result = await runOnce({
      agent: makeConfig(),
      prompt: 'hi',
      runViaSDK: runner,
    });
    expect(result).toBe('');
  });
});
