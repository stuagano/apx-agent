/**
 * Tests for resources.ts — ResourceSpec validation, tool annotation,
 * sub-agent URL translation, and the spec collector. Mirrors
 * python/tests/test_resources.py.
 *
 * Covers:
 *   1. ResourceSpec construction — rejects unknown kind + empty identifier,
 *      structural equality + freeze.
 *   2. attachResources / getResources — round-trip and append-not-replace.
 *   3. subAgentToEndpoint — endpoints/, serving-endpoints/, bare, URL,
 *      $VAR expansion (set + unset), databricksapps.com → null.
 *   4. collectResourceSpecs — model endpoint inclusion, tool resources,
 *      sub-agent endpoints, Apps URL skip, extras forwarding, dedup, and
 *      first-occurrence order (model → tools → sub-agents → extras).
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  attachResources,
  collectResourceSpecs,
  getResources,
  makeResourceSpec,
  subAgentToEndpoint,
  type ResourceSpec,
} from '../src/resources.js';

// ---------------------------------------------------------------------------
// ResourceSpec validation
// ---------------------------------------------------------------------------

describe('makeResourceSpec — validation', () => {
  it('rejects an unknown kind', () => {
    expect(() => makeResourceSpec('not_a_real_kind', 'x')).toThrow(/Unknown resource kind/);
  });

  it('rejects an empty identifier', () => {
    expect(() => makeResourceSpec('uc_function', '')).toThrow(/non-empty identifier/);
  });

  it('produces structurally equal specs for the same input', () => {
    const a = makeResourceSpec('uc_function', 'main.tools.f');
    const b = makeResourceSpec('uc_function', 'main.tools.f');
    expect(a).toEqual(b);
    expect(a.kind).toBe('uc_function');
    expect(a.identifier).toBe('main.tools.f');
  });

  it('freezes the returned spec', () => {
    const spec = makeResourceSpec('uc_function', 'main.tools.f');
    expect(Object.isFrozen(spec)).toBe(true);
    expect(() => {
      (spec as unknown as { kind: string }).kind = 'genie_space';
    }).toThrow();
  });
});

// ---------------------------------------------------------------------------
// attachResources / getResources
// ---------------------------------------------------------------------------

describe('attachResources / getResources', () => {
  it('returns [] for an un-annotated function', () => {
    const fn = (): string => 'noop';
    expect(getResources(fn)).toEqual([]);
  });

  it('round-trips a single attached spec', () => {
    const fn = (q: string): string => q;
    const spec = makeResourceSpec('uc_table', 'main.sales.orders');
    const ret = attachResources(fn, [spec]);
    expect(ret).toBe(fn);
    expect(getResources(fn)).toEqual([spec]);
  });

  it('appends, rather than replaces, on subsequent calls', () => {
    const fn = (q: string): string => q;
    attachResources(fn, [makeResourceSpec('uc_table', 'main.sales.orders')]);
    attachResources(fn, [makeResourceSpec('uc_function', 'main.tools.f')]);
    expect(getResources(fn)).toEqual([
      { kind: 'uc_table', identifier: 'main.sales.orders' },
      { kind: 'uc_function', identifier: 'main.tools.f' },
    ]);
  });

  it('returns a defensive copy (mutating the result does not affect storage)', () => {
    const fn = (): void => undefined;
    attachResources(fn, [makeResourceSpec('uc_function', 'main.tools.f')]);
    const out = getResources(fn);
    out.push(makeResourceSpec('uc_function', 'main.tools.g'));
    expect(getResources(fn)).toEqual([{ kind: 'uc_function', identifier: 'main.tools.f' }]);
  });

  it('tolerates null / undefined input', () => {
    expect(getResources(null)).toEqual([]);
    expect(getResources(undefined)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// subAgentToEndpoint
// ---------------------------------------------------------------------------

describe('subAgentToEndpoint — translation', () => {
  it.each([
    ['endpoints/data-triage', 'data-triage'],
    ['serving-endpoints/data-triage', 'data-triage'],
    ['data-triage', 'data-triage'],
    [
      'https://workspace.cloud.databricks.com/serving-endpoints/data-triage/invocations',
      'data-triage',
    ],
    ['https://workspace.cloud.databricks.com/serving-endpoints/data-triage', 'data-triage'],
  ])('translates %j → serving_endpoint(%j)', (raw, expected) => {
    const spec = subAgentToEndpoint(raw);
    expect(spec).not.toBeNull();
    expect(spec?.kind).toBe('serving_endpoint');
    expect(spec?.identifier).toBe(expected);
  });

  it.each([
    'https://data-triage-1234567890.cloud.databricksapps.com',
    'https://data-triage.workspace.databricksapps.com',
  ])('returns null for Apps URL %j', (raw) => {
    expect(subAgentToEndpoint(raw)).toBeNull();
  });

  describe('environment variable expansion', () => {
    const ENV_KEYS = ['SOME_AGENT_URL', 'DEFINITELY_UNSET_VAR_XYZ'] as const;
    const saved: Record<string, string | undefined> = {};

    beforeEach(() => {
      for (const key of ENV_KEYS) {
        saved[key] = process.env[key];
        delete process.env[key];
      }
    });

    afterEach(() => {
      for (const key of ENV_KEYS) {
        if (saved[key] === undefined) delete process.env[key];
        else process.env[key] = saved[key];
      }
    });

    it('expands $VAR to its value and continues translation', () => {
      process.env.SOME_AGENT_URL = 'endpoints/some-agent';
      expect(subAgentToEndpoint('$SOME_AGENT_URL')).toEqual({
        kind: 'serving_endpoint',
        identifier: 'some-agent',
      });
    });

    it('expands ${VAR} braces form too', () => {
      process.env.SOME_AGENT_URL = 'serving-endpoints/some-agent';
      expect(subAgentToEndpoint('${SOME_AGENT_URL}')).toEqual({
        kind: 'serving_endpoint',
        identifier: 'some-agent',
      });
    });

    it('returns null when $VAR is unset', () => {
      expect(subAgentToEndpoint('$DEFINITELY_UNSET_VAR_XYZ')).toBeNull();
    });

    it('returns null when an expanded $VAR contains an Apps URL', () => {
      process.env.SOME_AGENT_URL = 'https://x-1.cloud.databricksapps.com';
      expect(subAgentToEndpoint('$SOME_AGENT_URL')).toBeNull();
    });
  });
});

// ---------------------------------------------------------------------------
// collectResourceSpecs
// ---------------------------------------------------------------------------

/**
 * Fake agent for the collector tests — carries its own tool and sub-agent
 * lists. The iterators below just read them off. This sidesteps needing the
 * full agent class hierarchy in the unit test.
 */
interface FakeAgent {
  tools: unknown[];
  subAgents: string[];
}

const toolIterator = (a: FakeAgent): Iterable<unknown> => a.tools;
const subAgentIterator = (a: FakeAgent): Iterable<string> => a.subAgents;

function makeFakeAgent(opts: Partial<FakeAgent> = {}): FakeAgent {
  return { tools: opts.tools ?? [], subAgents: opts.subAgents ?? [] };
}

describe('collectResourceSpecs', () => {
  it('includes the model serving endpoint when supplied', () => {
    const agent = makeFakeAgent();
    const specs = collectResourceSpecs({
      agent,
      model: 'databricks-claude-sonnet-4-6',
      toolIterator,
      subAgentIterator,
    });
    expect(specs).toContainEqual({
      kind: 'serving_endpoint',
      identifier: 'databricks-claude-sonnet-4-6',
    });
  });

  it('omits the serving_endpoint when model is not supplied', () => {
    const ucFn = (): void => undefined;
    attachResources(ucFn, [makeResourceSpec('uc_function', 'main.tools.a')]);
    const specs = collectResourceSpecs({
      agent: makeFakeAgent({ tools: [ucFn] }),
      toolIterator,
      subAgentIterator,
    });
    expect(specs.filter((s) => s.kind === 'serving_endpoint')).toEqual([]);
  });

  it('gathers tool resources via the toolIterator', () => {
    const genieFn = (): void => undefined;
    attachResources(genieFn, [makeResourceSpec('genie_space', 'space-abc')]);
    const ucFn = (): void => undefined;
    attachResources(ucFn, [makeResourceSpec('uc_function', 'main.tools.classify')]);
    const plainFn = (): void => undefined;

    const specs = collectResourceSpecs({
      agent: makeFakeAgent({ tools: [genieFn, ucFn, plainFn] }),
      model: 'm',
      toolIterator,
      subAgentIterator,
    });

    expect(specs).toContainEqual({ kind: 'genie_space', identifier: 'space-abc' });
    expect(specs).toContainEqual({ kind: 'uc_function', identifier: 'main.tools.classify' });
    expect(specs).toContainEqual({ kind: 'serving_endpoint', identifier: 'm' });
  });

  it('includes sub-agent serving endpoints', () => {
    const specs = collectResourceSpecs({
      agent: makeFakeAgent({
        subAgents: ['endpoints/data-triage', 'serving-endpoints/billing'],
      }),
      model: 'm',
      toolIterator,
      subAgentIterator,
    });
    expect(specs).toContainEqual({ kind: 'serving_endpoint', identifier: 'data-triage' });
    expect(specs).toContainEqual({ kind: 'serving_endpoint', identifier: 'billing' });
  });

  it('skips Databricks Apps sub-agent URLs', () => {
    const specs = collectResourceSpecs({
      agent: makeFakeAgent({
        subAgents: ['https://data-triage-12345678.cloud.databricksapps.com'],
      }),
      model: 'm',
      toolIterator,
      subAgentIterator,
    });
    const endpointIdents = specs.filter((s) => s.kind === 'serving_endpoint').map((s) => s.identifier);
    expect(endpointIdents).toEqual(['m']);
  });

  it('forwards extras', () => {
    const specs = collectResourceSpecs({
      agent: makeFakeAgent(),
      model: 'm',
      extra: [makeResourceSpec('sql_warehouse', 'wh-12345')],
      toolIterator,
      subAgentIterator,
    });
    expect(specs).toContainEqual({ kind: 'sql_warehouse', identifier: 'wh-12345' });
  });

  it('dedupes repeated (kind, identifier) pairs, preserving the first', () => {
    const fnA = (): void => undefined;
    attachResources(fnA, [makeResourceSpec('uc_function', 'main.tools.same')]);
    const fnB = (): void => undefined;
    attachResources(fnB, [makeResourceSpec('uc_function', 'main.tools.same')]);

    const specs = collectResourceSpecs({
      agent: makeFakeAgent({ tools: [fnA, fnB] }),
      model: 'm',
      toolIterator,
      subAgentIterator,
    });
    const ucSpecs = specs.filter((s) => s.kind === 'uc_function');
    expect(ucSpecs).toEqual([{ kind: 'uc_function', identifier: 'main.tools.same' }]);
  });

  it('preserves the canonical order: model → tools → sub-agents → extras', () => {
    const genieFn = (): void => undefined;
    attachResources(genieFn, [makeResourceSpec('genie_space', 'space-1')]);
    const ucFn = (): void => undefined;
    attachResources(ucFn, [makeResourceSpec('uc_function', 'main.tools.a')]);

    const specs = collectResourceSpecs({
      agent: makeFakeAgent({
        tools: [genieFn, ucFn],
        subAgents: ['endpoints/triage'],
      }),
      model: 'm',
      extra: [makeResourceSpec('sql_warehouse', 'wh-1')],
      toolIterator,
      subAgentIterator,
    });
    expect(specs).toEqual([
      { kind: 'serving_endpoint', identifier: 'm' },
      { kind: 'genie_space', identifier: 'space-1' },
      { kind: 'uc_function', identifier: 'main.tools.a' },
      { kind: 'serving_endpoint', identifier: 'triage' },
      { kind: 'sql_warehouse', identifier: 'wh-1' },
    ]);
  });

  it('preserves tool iterator order across multiple tools', () => {
    const a = (): void => undefined;
    attachResources(a, [makeResourceSpec('genie_space', 'space-1')]);
    const b = (): void => undefined;
    attachResources(b, [makeResourceSpec('uc_function', 'main.tools.a')]);

    const specs = collectResourceSpecs({
      agent: makeFakeAgent({ tools: [a, b] }),
      model: 'm',
      toolIterator,
      subAgentIterator,
    });
    const kinds = specs.map((s) => s.kind);
    expect(kinds).toEqual(['serving_endpoint', 'genie_space', 'uc_function']);
  });

  it('honours the ResourceSpec input via the public interface', () => {
    // Direct object literal is allowed since ResourceSpec is just an interface.
    const spec: ResourceSpec = { kind: 'genie_space', identifier: 'space-1' };
    const fn = (): void => undefined;
    attachResources(fn, [spec]);
    const specs = collectResourceSpecs({
      agent: makeFakeAgent({ tools: [fn] }),
      toolIterator,
      subAgentIterator,
    });
    expect(specs).toContainEqual(spec);
  });
});
