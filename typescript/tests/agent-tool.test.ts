/**
 * Tests for agentTool() — the agent-as-tool composition primitive.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { z } from 'zod';
import { agentTool } from '../src/agent/agent-tool.js';
import type { AgentConfig, AgentExports } from '../src/agent/plugin.js';
import type { AgentTool } from '../src/agent/tools.js';

// ---------------------------------------------------------------------------
// Mock the runner so we don't actually hit any HTTP / model serving endpoints
// ---------------------------------------------------------------------------

vi.mock('../src/agent/runner.js', async () => {
  return {
    runViaSDK: vi.fn(async (params: { messages: { content: string }[] }) =>
      `runViaSDK called with: ${params.messages[0]?.content}`,
    ),
    callSubAgent: vi.fn(async (url: string, message: string) =>
      `callSubAgent(${url}) called with: ${message}`,
    ),
  };
});

import { runViaSDK, callSubAgent } from '../src/agent/runner.js';
import { runWithContext } from '../src/agent/request-context.js';

beforeEach(() => {
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Name + description inference
// ---------------------------------------------------------------------------

describe('agentTool — name inference', () => {
  it('uses explicit name over agent name', () => {
    const config: AgentConfig = { name: 'some_inner', model: 'm' };
    const tool = agentTool(config, { name: 'explicit' });
    expect(tool.name).toBe('explicit');
  });

  it('falls back to config.name', () => {
    const config: AgentConfig = { name: 'specialist', model: 'm' };
    const tool = agentTool(config);
    expect(tool.name).toBe('specialist');
  });

  it('falls back to generic name when config has none', () => {
    const config: AgentConfig = { model: 'm' };
    const tool = agentTool(config);
    expect(tool.name).toBe('agent');
  });

  it('extracts name from URL for remote agents', () => {
    const tool = agentTool('https://billing-agent.workspace.databricksapps.com');
    expect(tool.name).toBeTruthy();
    expect(tool.name).not.toBe('agent');
  });
});

// ---------------------------------------------------------------------------
// Shape — produces a valid AgentTool
// ---------------------------------------------------------------------------

describe('agentTool — shape', () => {
  it('returns an AgentTool with the expected fields', () => {
    const config: AgentConfig = { name: 'spec', model: 'm' };
    const tool: AgentTool = agentTool(config, { description: 'd' });
    expect(tool.name).toBe('spec');
    expect(tool.description).toBe('d');
    expect(tool.parameters).toBeDefined();
    expect(typeof tool.handler).toBe('function');
  });

  it('parameters schema includes a message string', () => {
    const tool = agentTool({ model: 'm' });
    const parsed = (tool.parameters as z.ZodType).safeParse({ message: 'hello' });
    expect(parsed.success).toBe(true);
  });

  it('parameters schema rejects missing message', () => {
    const tool = agentTool({ model: 'm' });
    const parsed = (tool.parameters as z.ZodType).safeParse({});
    expect(parsed.success).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Behavior — local AgentConfig delegates to runViaSDK
// ---------------------------------------------------------------------------

describe('agentTool — local AgentConfig invocation', () => {
  it('invokes runViaSDK for an AgentConfig target', async () => {
    const config: AgentConfig = {
      name: 'spec',
      model: 'databricks-claude-sonnet-4-6',
      instructions: 'You are a specialist.',
      tools: [],
    };
    const tool = agentTool(config);
    const result = await runWithContext(
      { oboHeaders: { 'X-Forwarded-Access-Token': 'tkn' } },
      () => tool.handler({ message: 'do the thing' }) as Promise<string>,
    );

    expect(callSubAgent).not.toHaveBeenCalled();
    expect(runViaSDK).toHaveBeenCalledTimes(1);
    const args = (runViaSDK as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.model).toBe('databricks-claude-sonnet-4-6');
    expect(args.instructions).toBe('You are a specialist.');
    expect(args.messages).toEqual([{ role: 'user', content: 'do the thing' }]);
    expect(args.oboHeaders).toEqual({ 'X-Forwarded-Access-Token': 'tkn' });
    expect(result).toContain('do the thing');
  });
});

// ---------------------------------------------------------------------------
// Behavior — AgentExports delegates to runViaSDK using getConfig + getTools
// ---------------------------------------------------------------------------

describe('agentTool — AgentExports invocation', () => {
  it('invokes runViaSDK with config from getConfig and tools from getTools', async () => {
    const fakeTools = [
      { name: 't1', description: '', parameters: z.object({}), handler: async () => '' },
    ];
    const fakeExports: AgentExports = {
      getConfig: () => ({ name: 'spec', model: 'm', instructions: 'inst' }),
      getTools: () => fakeTools as any,
      getToolSchemas: () => [],
    };

    const tool = agentTool(fakeExports);
    await runWithContext(
      { oboHeaders: {} },
      () => tool.handler({ message: 'hello' }) as Promise<string>,
    );

    expect(runViaSDK).toHaveBeenCalledTimes(1);
    const args = (runViaSDK as unknown as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(args.tools).toBe(fakeTools);
    expect(args.instructions).toBe('inst');
  });
});

// ---------------------------------------------------------------------------
// Behavior — URL delegates to callSubAgent
// ---------------------------------------------------------------------------

describe('agentTool — remote URL invocation', () => {
  it('invokes callSubAgent for a URL target', async () => {
    const url = 'https://billing.example.databricksapps.com';
    const tool = agentTool(url, { name: 'billing' });
    const result = await runWithContext(
      { oboHeaders: { Authorization: 'Bearer x' } },
      () => tool.handler({ message: 'why was my bill so high' }) as Promise<string>,
    );

    expect(runViaSDK).not.toHaveBeenCalled();
    expect(callSubAgent).toHaveBeenCalledTimes(1);
    const args = (callSubAgent as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(args[0]).toBe(url);
    expect(args[1]).toBe('why was my bill so high');
    expect(args[2]).toEqual({ Authorization: 'Bearer x' });
    expect(result).toContain(url);
  });
});
