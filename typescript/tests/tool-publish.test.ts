/**
 * Tests for tool-publish.ts — publishToolsToUc.
 *
 * Mirrors python/tests/test_tool_publish.py. Covers:
 *
 *   1. Walks tools, publishes those with UC metadata, ignores plain tools.
 *   2. Idempotent enumeration — same ucName twice → publish once.
 *   3. dryRun skips writes and returns descriptive results.
 *   4. Grants applied per-principal via workspaceClient.apiRequest with the
 *      Unity Catalog permissions API path.
 *   5. createPythonFunction failure surfaces as a thrown Error.
 *   6. Grant failure logs-and-continues (the failed principal is dropped).
 *
 * Skipped (with rationale) vs. Python:
 *
 *   - Tree-walking through SequentialAgent — TS `publishToolsToUc` takes a
 *     tool list directly (see tool-publish.ts module header). The
 *     "duplicate tool entry → one publish" case below covers the dedup
 *     semantics; tree-walking is the caller's responsibility.
 *   - Explicit `tool_id` / `extra_tool_kwargs` — Python's `@tool` accepts
 *     neither. There's no analogue to add.
 */

import { describe, it, expect, vi } from 'vitest';
import { z } from 'zod';

import type { AgentTool } from '../src/agent/tools.js';
import { defineUcTool } from '../src/tool.js';
import {
  publishToolsToUc,
  type PublishResult,
  type UcFunctionClient,
  type WsClient,
} from '../src/tool-publish.js';

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

interface CreateCall {
  catalog: string;
  schema: string;
  functionName: string;
  replace: boolean;
}

interface FakeUcClient extends UcFunctionClient {
  readonly calls: CreateCall[];
}

function makeFakeUcClient(opts: { failOn?: Set<string> } = {}): FakeUcClient {
  const calls: CreateCall[] = [];
  const failOn = opts.failOn ?? new Set<string>();

  return {
    calls,
    async createPythonFunction(args) {
      const tool = args.func as AgentTool;
      const full = `${args.catalog}.${args.schema}.${tool.name}`;
      if (failOn.has(full)) {
        throw new Error(`simulated failure for ${full}`);
      }
      calls.push({
        catalog: args.catalog,
        schema: args.schema,
        functionName: tool.name,
        replace: args.replace ?? true,
      });
    },
  };
}

interface GrantCall {
  method: string;
  path: string;
  body?: object;
}

interface FakeWsClient extends WsClient {
  readonly calls: GrantCall[];
}

function makeFakeWsClient(opts: { grantFailures?: Set<string> } = {}): FakeWsClient {
  const calls: GrantCall[] = [];
  // Failures keyed by `${ucName}|${principal}`
  const grantFailures = opts.grantFailures ?? new Set<string>();

  return {
    calls,
    async apiRequest(method: string, path: string, body?: object) {
      calls.push({ method, path, body });
      if (method !== 'PATCH') {
        throw new Error(`expected PATCH, got ${method}`);
      }
      const prefix = '/api/2.1/unity-catalog/permissions/function/';
      if (!path.startsWith(prefix)) {
        throw new Error(`unexpected path ${path}`);
      }
      const ucName = path.slice(prefix.length);
      const changes = (body as { changes: Array<{ principal: string; add: string[] }> }).changes;
      for (const change of changes) {
        if (grantFailures.has(`${ucName}|${change.principal}`)) {
          throw new Error(`simulated grant failure for ${ucName}/${change.principal}`);
        }
      }
    },
  };
}

// ---------------------------------------------------------------------------
// Tool fixtures
// ---------------------------------------------------------------------------

function makeClassify(): AgentTool {
  return defineUcTool({
    name: 'classify_intent',
    description: 'Classify a customer query.',
    parameters: z.object({ query: z.string() }),
    handler: async ({ query }) => (query.toLowerCase().includes('bill') ? 'billing' : 'other'),
    uc: 'main.tools.classify_intent',
    grant: ['agent_consumers'],
  });
}

function makeScore(): AgentTool {
  return defineUcTool({
    name: 'score_customer',
    description: 'Compute a customer score.',
    parameters: z.object({ customer_id: z.string() }),
    handler: async () => 42,
    uc: 'main.tools.score_customer',
    grant: ['agent_consumers', 'data_team'],
  });
}

function makePlain(): AgentTool {
  // No UC metadata — `publishToolsToUc` should skip this.
  return {
    name: 'plain',
    description: 'plain',
    parameters: z.object({}),
    handler: async () => 'noop',
  };
}

// ---------------------------------------------------------------------------
// 1 — Walking
// ---------------------------------------------------------------------------

describe('publishToolsToUc — tool walking', () => {
  it('publishes UC tools and ignores plain ones', async () => {
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient();

    const results = await publishToolsToUc({
      tools: [makeClassify(), makeScore(), makePlain()],
      ucClient,
      workspaceClient,
    });

    expect(results).toHaveLength(2);
    const ucNames = new Set(results.map((r) => r.ucName));
    expect(ucNames).toEqual(new Set(['main.tools.classify_intent', 'main.tools.score_customer']));
    expect(ucClient.calls).toHaveLength(2);
  });

  it('passes split (catalog, schema) to createPythonFunction', async () => {
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient();

    await publishToolsToUc({
      tools: [makeClassify()],
      ucClient,
      workspaceClient,
    });

    expect(ucClient.calls).toHaveLength(1);
    expect(ucClient.calls[0].catalog).toBe('main');
    expect(ucClient.calls[0].schema).toBe('tools');
    expect(ucClient.calls[0].functionName).toBe('classify_intent');
    expect(ucClient.calls[0].replace).toBe(true);
  });

  it('returns [] when no tools carry UC metadata', async () => {
    const ucClient = makeFakeUcClient();
    const results = await publishToolsToUc({
      tools: [makePlain()],
      ucClient,
    });
    expect(results).toEqual([]);
    expect(ucClient.calls).toEqual([]);
  });

  it('forwards replace: false when requested', async () => {
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient();

    await publishToolsToUc({
      tools: [makeClassify()],
      ucClient,
      workspaceClient,
      replace: false,
    });

    expect(ucClient.calls[0].replace).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2 — Idempotent enumeration
// ---------------------------------------------------------------------------

describe('publishToolsToUc — idempotent enumeration', () => {
  it('publishes a duplicated ucName only once', async () => {
    const classify = makeClassify();
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient();

    // Same tool object passed twice (and once again — paranoia about Set dedup).
    const results = await publishToolsToUc({
      tools: [classify, classify, classify],
      ucClient,
      workspaceClient,
    });

    expect(results).toHaveLength(1);
    expect(ucClient.calls).toHaveLength(1);
  });

  it('publishes two distinct tools that happen to share a ucName only once', async () => {
    // Two tool objects with the same UC name — simulates two agent branches
    // declaring the same UC function.
    const a = defineUcTool({
      name: 'dup',
      description: 'a',
      parameters: z.object({ x: z.string() }),
      handler: async ({ x }) => x,
      uc: 'main.tools.dup',
    });
    const b = defineUcTool({
      name: 'dup',
      description: 'b',
      parameters: z.object({ x: z.string() }),
      handler: async ({ x }) => x,
      uc: 'main.tools.dup',
    });
    const ucClient = makeFakeUcClient();

    const results = await publishToolsToUc({
      tools: [a, b],
      ucClient,
    });

    expect(results).toHaveLength(1);
    expect(ucClient.calls).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 3 — dryRun
// ---------------------------------------------------------------------------

describe('publishToolsToUc — dryRun', () => {
  it('skips writes and returns dry_run results', async () => {
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient();

    const results = await publishToolsToUc({
      tools: [makeClassify()],
      ucClient,
      workspaceClient,
      dryRun: true,
    });

    expect(results).toHaveLength(1);
    expect(results[0].skipped).toBe(true);
    expect(results[0].skipReason).toBe('dry_run');
    expect(ucClient.calls).toEqual([]);
    expect(workspaceClient.calls).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 4 — Grants
// ---------------------------------------------------------------------------

describe('publishToolsToUc — grants', () => {
  it('applies grants per principal via the UC permissions API', async () => {
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient();

    const results = await publishToolsToUc({
      tools: [makeScore()], // two grants
      ucClient,
      workspaceClient,
    });

    expect(results[0].grantsApplied).toEqual(['agent_consumers', 'data_team']);
    expect(workspaceClient.calls).toHaveLength(2);
    for (const call of workspaceClient.calls) {
      expect(call.method).toBe('PATCH');
      expect(call.path).toBe('/api/2.1/unity-catalog/permissions/function/main.tools.score_customer');
    }
  });

  it('records principals in the order they were declared', async () => {
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient();

    const results = await publishToolsToUc({
      tools: [makeScore()],
      ucClient,
      workspaceClient,
    });

    const principals = workspaceClient.calls.map((c) => {
      const body = c.body as { changes: Array<{ principal: string }> };
      return body.changes[0].principal;
    });
    expect(principals).toEqual(['agent_consumers', 'data_team']);
    expect(results[0].grantsApplied).toEqual(['agent_consumers', 'data_team']);
  });

  it('requires a workspaceClient when any tool declares grants', async () => {
    const ucClient = makeFakeUcClient();

    await expect(
      publishToolsToUc({
        tools: [makeClassify()], // declares a grant
        ucClient,
        // no workspaceClient
      }),
    ).rejects.toThrow(/workspaceClient/);
  });

  it('does not require a workspaceClient when no tool declares grants', async () => {
    const noGrants = defineUcTool({
      name: 'no_grant',
      description: 'No grants',
      parameters: z.object({ x: z.string() }),
      handler: async ({ x }) => x,
      uc: 'main.tools.no_grant',
    });
    const ucClient = makeFakeUcClient();

    const results = await publishToolsToUc({ tools: [noGrants], ucClient });

    expect(results).toHaveLength(1);
    expect(results[0].grantsApplied).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 5 — create failure surfaces as Error
// ---------------------------------------------------------------------------

describe('publishToolsToUc — create failure', () => {
  it('throws when createPythonFunction fails', async () => {
    const ucClient = makeFakeUcClient({
      failOn: new Set(['main.tools.classify_intent']),
    });
    const workspaceClient = makeFakeWsClient();

    await expect(
      publishToolsToUc({
        tools: [makeClassify()],
        ucClient,
        workspaceClient,
      }),
    ).rejects.toThrow(/Failed to publish/);
  });
});

// ---------------------------------------------------------------------------
// 6 — Grant failure is logged-and-continues
// ---------------------------------------------------------------------------

describe('publishToolsToUc — grant failure', () => {
  it('drops a failed principal but completes the publish', async () => {
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient({
      grantFailures: new Set(['main.tools.score_customer|data_team']),
    });
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => undefined);

    try {
      const results = await publishToolsToUc({
        tools: [makeScore()],
        ucClient,
        workspaceClient,
      });

      expect(results).toHaveLength(1);
      expect(results[0].grantsApplied).toEqual(['agent_consumers']);
      expect(ucClient.calls).toHaveLength(1);
      expect(warnSpy).toHaveBeenCalled();
    } finally {
      warnSpy.mockRestore();
    }
  });
});

// ---------------------------------------------------------------------------
// PublishResult shape
// ---------------------------------------------------------------------------

describe('PublishResult', () => {
  it('is frozen at construction', async () => {
    const ucClient = makeFakeUcClient();
    const workspaceClient = makeFakeWsClient();
    const results = await publishToolsToUc({
      tools: [makeClassify()],
      ucClient,
      workspaceClient,
    });

    const r = results[0] as PublishResult;
    expect(Object.isFrozen(r)).toBe(true);
    expect(() => {
      (r as unknown as { ucName: string }).ucName = 'different';
    }).toThrow();
  });
});
