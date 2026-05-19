/**
 * Tests for tool.ts — `defineUcTool`, `ToolMetadata`, and `getToolMetadata`.
 *
 * Mirrors python/tests/test_tool.py. Covers:
 *
 *   1. defineUcTool validates the three-part UC name + empty segments.
 *   2. defineUcTool produces an AgentTool with a `uc_function` ResourceSpec
 *      reachable via getResources.
 *   3. ToolMetadata.ucName / grants / overrides are attached and retrievable
 *      via getToolMetadata.
 *   4. The returned tool runs end-to-end (parameters parsed, handler called).
 *   5. Metadata object is frozen.
 *   6. getToolMetadata returns null for un-marked objects.
 *
 * Skipped (with rationale) vs. the Python tests:
 *
 *   - "Bare @tool" form — TS has no bare decorator equivalent; the base
 *     `defineTool` already produces an AgentTool without UC metadata.
 *   - "grant without uc raises" — `defineUcTool` makes `uc` mandatory at
 *     compile time. `grant`-without-`uc` is structurally impossible.
 *   - "Dependencies rejection" — TS has no Dependencies injection mechanism
 *     (documented in tool.ts module header). Skipped.
 */

import { describe, it, expect, vi } from 'vitest';
import { z } from 'zod';

import { defineUcTool, getToolMetadata, type ToolMetadata } from '../src/tool.js';
import { getResources, makeResourceSpec } from '../src/resources.js';

// ---------------------------------------------------------------------------
// UC name validation
// ---------------------------------------------------------------------------

describe('defineUcTool — UC name validation', () => {
  it('rejects a two-part UC name', () => {
    expect(() =>
      defineUcTool({
        name: 'fn',
        description: 'doc',
        parameters: z.object({ x: z.string() }),
        handler: async ({ x }) => x,
        uc: 'too.short',
      }),
    ).toThrow(/three-part UC name/);
  });

  it('rejects a four-part UC name', () => {
    expect(() =>
      defineUcTool({
        name: 'fn',
        description: 'doc',
        parameters: z.object({ x: z.string() }),
        handler: async ({ x }) => x,
        uc: 'a.b.c.d',
      }),
    ).toThrow(/three-part UC name/);
  });

  it('rejects an empty segment', () => {
    expect(() =>
      defineUcTool({
        name: 'fn',
        description: 'doc',
        parameters: z.object({ x: z.string() }),
        handler: async ({ x }) => x,
        uc: 'main..fn',
      }),
    ).toThrow(/empty segment/);
  });

  it('rejects an empty uc string outright', () => {
    expect(() =>
      defineUcTool({
        name: 'fn',
        description: 'doc',
        parameters: z.object({ x: z.string() }),
        handler: async ({ x }) => x,
        uc: '',
      }),
    ).toThrow();
  });

  it('accepts a well-formed three-part UC name', () => {
    const tool = defineUcTool({
      name: 'pure',
      description: 'pure',
      parameters: z.object({ x: z.string() }),
      handler: async ({ x }) => x,
      uc: 'main.tools.pure',
    });
    const meta = getToolMetadata(tool);
    expect(meta).not.toBeNull();
    expect(meta?.ucName).toBe('main.tools.pure');
  });
});

// ---------------------------------------------------------------------------
// ToolMetadata attachment
// ---------------------------------------------------------------------------

describe('defineUcTool — ToolMetadata attachment', () => {
  it('attaches ucName and grants to the tool', () => {
    const tool = defineUcTool({
      name: 'classify',
      description: 'Classify.',
      parameters: z.object({ query: z.string() }),
      handler: async ({ query }) => query,
      uc: 'main.tools.classify',
      grant: ['agent_consumers', 'data_team'],
    });

    const meta = getToolMetadata(tool);
    expect(meta).not.toBeNull();
    expect(meta?.ucName).toBe('main.tools.classify');
    expect(meta?.grants).toEqual(['agent_consumers', 'data_team']);
  });

  it('defaults grants to an empty list', () => {
    const tool = defineUcTool({
      name: 'no_grant',
      description: 'No grants',
      parameters: z.object({ x: z.string() }),
      handler: async ({ x }) => x,
      uc: 'main.tools.no_grant',
    });

    const meta = getToolMetadata(tool);
    expect(meta).not.toBeNull();
    expect(meta?.grants).toEqual([]);
  });

  it('records name and description overrides on the metadata', () => {
    const tool = defineUcTool({
      name: 'renamed',
      description: 'Overridden description.',
      parameters: z.object({ x: z.string() }),
      handler: async ({ x }) => x,
      uc: 'main.tools.renamed',
    });

    expect(tool.name).toBe('renamed');
    expect(tool.description).toBe('Overridden description.');

    const meta = getToolMetadata(tool);
    expect(meta?.nameOverride).toBe('renamed');
    expect(meta?.descriptionOverride).toBe('Overridden description.');
  });

  it('freezes the ToolMetadata object', () => {
    const tool = defineUcTool({
      name: 'fn',
      description: 'doc',
      parameters: z.object({ x: z.string() }),
      handler: async ({ x }) => x,
      uc: 'main.tools.f',
    });

    const meta = getToolMetadata(tool) as ToolMetadata;
    expect(Object.isFrozen(meta)).toBe(true);
    expect(() => {
      (meta as unknown as { ucName: string }).ucName = 'different';
    }).toThrow();
  });
});

// ---------------------------------------------------------------------------
// Auto-attached uc_function ResourceSpec
// ---------------------------------------------------------------------------

describe('defineUcTool — auto-attached resource', () => {
  it('attaches a uc_function ResourceSpec retrievable via getResources', () => {
    const tool = defineUcTool({
      name: 'classify_intent',
      description: 'doc',
      parameters: z.object({ query: z.string() }),
      handler: async ({ query }) => query,
      uc: 'main.tools.classify_intent',
    });

    const specs = getResources(tool);
    expect(specs).toContainEqual(makeResourceSpec('uc_function', 'main.tools.classify_intent'));
  });
});

// ---------------------------------------------------------------------------
// Tool execution
// ---------------------------------------------------------------------------

describe('defineUcTool — runtime behaviour', () => {
  it('parses parameters and invokes the handler', async () => {
    const handler = vi.fn(async ({ count }: { count: number }) => count * 3);
    const tool = defineUcTool({
      name: 'tripler',
      description: 'Triples a number',
      parameters: z.object({ count: z.number() }),
      handler,
      uc: 'main.tools.tripler',
    });

    const out = await tool.handler({ count: 4 });
    expect(out).toBe(12);
    expect(handler).toHaveBeenCalledWith({ count: 4 });
  });

  it('rejects invalid input via the Zod parser', async () => {
    const tool = defineUcTool({
      name: 'strict',
      description: 'doc',
      parameters: z.object({ s: z.string().min(1) }),
      handler: async ({ s }) => s,
      uc: 'main.tools.strict',
    });

    await expect(tool.handler({ s: '' })).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// getToolMetadata on un-marked objects
// ---------------------------------------------------------------------------

describe('getToolMetadata', () => {
  it('returns null for an object that was never run through defineUcTool', () => {
    const plain = { name: 'plain', description: 'doc', parameters: z.object({}), handler: async () => null };
    expect(getToolMetadata(plain)).toBeNull();
  });

  it('returns null for null / undefined input', () => {
    expect(getToolMetadata(null)).toBeNull();
    expect(getToolMetadata(undefined)).toBeNull();
  });
});
