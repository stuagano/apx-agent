/**
 * Tests for watchdog.ts — databricks-watchdog integration.
 *
 * Mirrors python/tests/test_watchdog.py.
 *
 * Covers:
 *   - WatchdogDecision defaults / frozen
 *   - WatchdogClient.evaluate happy path + transport-failure fail-open +
 *     non-object fallback
 *   - WatchdogClient.reportViolation shape + failure tolerance
 *   - WatchdogGuard adapters (forInput / forOutput / forTool / forModel)
 *   - reject decisions short-circuit; redact rewrites; allow passes through
 *   - violation reports fire on reject + redact, not on allow
 *   - getActiveSpan: audit attributes recorded on non-allow decisions
 *   - emitAgentMetadata full shape
 *   - setUcTagsForAgent writes apx.agent.* via mlflowClient + truncates + tolerates failures
 *   - makeUcViolationWriter: bad-path rejection, INSERT shape, single-quote escape,
 *     pass-through for evaluate, auto-create caching
 *   - makeMcpTransport: calls mcpClient with right args, falls back to allow on error
 *   - makeWatchdogTransport: routes by type
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  WatchdogClient,
  WatchdogGuard,
  emitAgentMetadata,
  makeMcpTransport,
  makeUcViolationWriter,
  makeWatchdogDecision,
  makeWatchdogTransport,
  noOpTransport,
  setUcTagsForAgent,
  type McpToolCallFn,
  type MlflowTagClient,
  type SqlExecutor,
  type ToolDescriptor,
  type WatchdogDecision,
} from '../src/watchdog.js';
import { attachResources, makeResourceSpec } from '../src/resources.js';
import type { SpanLike } from '../src/audit.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type RecordedTransport = ((req: object) => Promise<object>) & { calls: object[] };

function makeRecordedTransport(
  responses: Record<string, Record<string, unknown>> = {},
): RecordedTransport {
  const calls: object[] = [];
  const fn = (async (req: object) => {
    calls.push(req);
    const r = req as Record<string, unknown>;
    if (r.type === 'violation_report') {
      return responses['violation_report'] ?? {};
    }
    const op = typeof r.operation === 'string' ? r.operation : '';
    return responses[op] ?? { action: 'allow' };
  }) as RecordedTransport;
  fn.calls = calls;
  return fn;
}

function silenceWarn(): void {
  beforeEach(() => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });
}

function mockSpan(): SpanLike & { calls: Array<[string, unknown]> } {
  const calls: Array<[string, unknown]> = [];
  return {
    calls,
    setAttribute(key: string, value: unknown) {
      calls.push([key, value]);
    },
  };
}

// ---------------------------------------------------------------------------
// WatchdogDecision
// ---------------------------------------------------------------------------

describe('WatchdogDecision', () => {
  it('defaults to allow', () => {
    const d = makeWatchdogDecision();
    expect(d.action).toBe('allow');
    expect(d.reason).toBeUndefined();
    expect(d.metadata).toEqual({});
  });

  it('is frozen', () => {
    const d = makeWatchdogDecision({ action: 'reject', reason: 'nope' });
    expect(Object.isFrozen(d)).toBe(true);
    expect(() => {
      (d as unknown as { action: string }).action = 'allow';
    }).toThrow();
  });

  it('freezes metadata too', () => {
    const d = makeWatchdogDecision({ action: 'reject', metadata: { k: 'v' } });
    expect(Object.isFrozen(d.metadata)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// WatchdogClient.evaluate
// ---------------------------------------------------------------------------

describe('WatchdogClient.evaluate', () => {
  silenceWarn();

  it('default client (noOpTransport) allows everything', async () => {
    const client = new WatchdogClient();
    const d = await client.evaluate({ operation: 'tool_call', context: { tool_name: 'x' } });
    expect(d.action).toBe('allow');
  });

  it('parses a transport response with reject + reason + policy', async () => {
    const transport = makeRecordedTransport({
      tool_call: {
        action: 'reject',
        reason: 'PII tool access denied',
        policy_id: 'pii-strict-001',
        domain: 'security',
        metadata: { owner: 'data-team@x.com' },
      },
    });
    const client = new WatchdogClient({ transport });
    const d = await client.evaluate({ operation: 'tool_call', context: { tool_name: 'leak_pii' } });
    expect(d.action).toBe('reject');
    expect(d.reason).toBe('PII tool access denied');
    expect(d.policyId).toBe('pii-strict-001');
    expect(d.domain).toBe('security');
    expect(d.metadata).toEqual({ owner: 'data-team@x.com' });
  });

  it('falls back to allow on transport exception', async () => {
    const client = new WatchdogClient({
      transport: async () => {
        throw new Error('network down');
      },
    });
    const d = await client.evaluate({ operation: 'tool_call' });
    expect(d.action).toBe('allow');
    expect(d.reason).toContain('transport error');
  });

  it('falls back to allow when transport returns a non-object', async () => {
    const client = new WatchdogClient({
      transport: async () => 'garbage' as unknown as object,
    });
    const d = await client.evaluate({ operation: 'tool_call' });
    expect(d.action).toBe('allow');
  });

  it('falls back to allow when transport returns an array (Array.isArray check)', async () => {
    const client = new WatchdogClient({
      transport: async () => [] as unknown as object,
    });
    const d = await client.evaluate({ operation: 'tool_call' });
    expect(d.action).toBe('allow');
  });

  it('forwards operation and context to the transport', async () => {
    const transport = makeRecordedTransport();
    const client = new WatchdogClient({ transport });
    await client.evaluate({ operation: 'input_message', context: { agent_name: 'triage' } });
    expect(transport.calls[0]).toEqual({
      operation: 'input_message',
      context: { agent_name: 'triage' },
    });
  });

  it('treats unknown action strings as allow', async () => {
    const client = new WatchdogClient({
      transport: async () => ({ action: 'lolnope' }),
    });
    const d = await client.evaluate({ operation: 'tool_call' });
    expect(d.action).toBe('allow');
  });
});

// ---------------------------------------------------------------------------
// WatchdogClient.reportViolation
// ---------------------------------------------------------------------------

describe('WatchdogClient.reportViolation', () => {
  silenceWarn();

  it('sends the expected shape to the transport', async () => {
    const transport = makeRecordedTransport();
    const client = new WatchdogClient({ transport });
    const decision = makeWatchdogDecision({
      action: 'reject',
      reason: 'bad',
      policyId: 'p-1',
      domain: 'security',
      metadata: { k: 'v' },
    });
    await client.reportViolation(decision, { tool_name: 'leak_pii' });
    const sent = transport.calls[0] as Record<string, unknown>;
    expect(sent.type).toBe('violation_report');
    const d = sent.decision as Record<string, unknown>;
    expect(d.policy_id).toBe('p-1');
    expect(sent.context).toEqual({ tool_name: 'leak_pii' });
  });

  it('swallows transport errors', async () => {
    const client = new WatchdogClient({
      transport: async () => {
        throw new Error('network down');
      },
    });
    await expect(
      client.reportViolation(makeWatchdogDecision({ action: 'reject' })),
    ).resolves.toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// WatchdogGuard.forInput
// ---------------------------------------------------------------------------

describe('WatchdogGuard.forInput', () => {
  silenceWarn();

  it('returns null when decision is allow', async () => {
    const transport = makeRecordedTransport();
    const guard = new WatchdogGuard({
      client: new WatchdogClient({ transport }),
      agentName: 'triage',
    });
    const out = await guard.forInput()([{ role: 'user', content: 'hi' }]);
    expect(out).toBeNull();
  });

  it('returns reason on reject and reports violation', async () => {
    const transport = makeRecordedTransport({
      input_message: { action: 'reject', reason: 'PII in input' },
    });
    const guard = new WatchdogGuard({
      client: new WatchdogClient({ transport }),
      agentName: 'triage',
    });
    const result = await guard.forInput()([{ role: 'user', content: 'my ssn is 123-45-6789' }]);
    expect(result).toBe('PII in input');
    // First evaluate, then report_violation
    expect(transport.calls).toHaveLength(2);
    expect((transport.calls[1] as Record<string, unknown>).type).toBe('violation_report');
  });

  it('threads agent_name into the context', async () => {
    const transport = makeRecordedTransport();
    const guard = new WatchdogGuard({
      client: new WatchdogClient({ transport }),
      agentName: 'triage',
    });
    await guard.forInput()([{ role: 'user', content: 'hi' }]);
    const ctx = (transport.calls[0] as Record<string, unknown>).context as Record<string, unknown>;
    expect(ctx.agent_name).toBe('triage');
  });
});

// ---------------------------------------------------------------------------
// WatchdogGuard.forOutput
// ---------------------------------------------------------------------------

describe('WatchdogGuard.forOutput', () => {
  silenceWarn();

  it('redact replaces text with redactedContent', async () => {
    const transport = makeRecordedTransport({
      output_message: {
        action: 'redact',
        reason: 'PII detected',
        redacted_content: 'Redacted content.',
      },
    });
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    const out = await guard.forOutput()('Your SSN is 123-45-6789.');
    expect(out).toBe('Redacted content.');
  });

  it('redact without redactedContent passes through (null)', async () => {
    const transport = makeRecordedTransport({
      output_message: { action: 'redact', reason: 'x' },
    });
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    const out = await guard.forOutput()('hello');
    expect(out).toBeNull();
  });

  it('reject returns the reason', async () => {
    const transport = makeRecordedTransport({
      output_message: { action: 'reject', reason: 'policy violation' },
    });
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    const out = await guard.forOutput()('anything');
    expect(out).toBe('policy violation');
  });
});

// ---------------------------------------------------------------------------
// WatchdogGuard.forTool
// ---------------------------------------------------------------------------

describe('WatchdogGuard.forTool', () => {
  silenceWarn();

  it('allow does not throw', async () => {
    const transport = makeRecordedTransport();
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    await expect(guard.forTool()('classify_intent', { query: 'x' })).resolves.toBeUndefined();
  });

  it('reject throws with the reason', async () => {
    const transport = makeRecordedTransport({
      tool_call: { action: 'reject', reason: 'tool denied' },
    });
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    await expect(guard.forTool()('classify_intent', { query: 'x' })).rejects.toThrow('tool denied');
  });

  it('passes tool_name + arguments through the context', async () => {
    const transport = makeRecordedTransport();
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    await guard.forTool()('my_tool', { arg: 1 });
    const ctx = (transport.calls[0] as Record<string, unknown>).context as Record<string, unknown>;
    expect(ctx.tool_name).toBe('my_tool');
    expect(ctx.arguments).toEqual({ arg: 1 });
  });
});

// ---------------------------------------------------------------------------
// WatchdogGuard.forModel
// ---------------------------------------------------------------------------

describe('WatchdogGuard.forModel', () => {
  silenceWarn();

  it('allow does not throw', async () => {
    const transport = makeRecordedTransport();
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    await expect(guard.forModel()([['msg']])).resolves.toBeUndefined();
  });

  it('reject throws with the reason', async () => {
    const transport = makeRecordedTransport({
      model_call: { action: 'reject', reason: 'model budget exceeded' },
    });
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    await expect(guard.forModel()(['prompt'])).rejects.toThrow('model budget exceeded');
  });
});

// ---------------------------------------------------------------------------
// Audit-attr wiring: getActiveSpan injection
// ---------------------------------------------------------------------------

describe('WatchdogGuard — audit attributes via getActiveSpan', () => {
  silenceWarn();

  it('reject records apx.watchdog.* + apx.agent.name on the active span', async () => {
    const transport = makeRecordedTransport({
      tool_call: {
        action: 'reject',
        reason: 'blocked',
        policy_id: 'p-1',
        domain: 'security',
      },
    });
    const span = mockSpan();
    const guard = new WatchdogGuard({
      client: new WatchdogClient({ transport }),
      agentName: 'triage',
      getActiveSpan: () => span,
    });
    await expect(guard.forTool()('classify_intent', { query: 'x' })).rejects.toThrow('blocked');
    const keys = span.calls.map((c) => c[0]);
    expect(keys).toContain('apx.watchdog.action');
    expect(keys).toContain('apx.watchdog.reason');
    expect(keys).toContain('apx.watchdog.policy_id');
    expect(keys).toContain('apx.watchdog.domain');
    expect(keys).toContain('apx.agent.name');
  });

  it('allow does NOT record any apx.watchdog.* attributes', async () => {
    const transport = makeRecordedTransport(); // default allow
    const span = mockSpan();
    const guard = new WatchdogGuard({
      client: new WatchdogClient({ transport }),
      getActiveSpan: () => span,
    });
    await guard.forTool()('classify_intent', {});
    const keys = span.calls.map((c) => c[0]);
    expect(keys).not.toContain('apx.watchdog.action');
  });

  it('redact records audit attrs on the active span', async () => {
    const transport = makeRecordedTransport({
      output_message: {
        action: 'redact',
        reason: 'PII',
        redacted_content: '<redacted>',
        policy_id: 'p-2',
      },
    });
    const span = mockSpan();
    const guard = new WatchdogGuard({
      client: new WatchdogClient({ transport }),
      agentName: 'triage',
      getActiveSpan: () => span,
    });
    const out = await guard.forOutput()('text with PII');
    expect(out).toBe('<redacted>');
    const keys = span.calls.map((c) => c[0]);
    expect(keys).toContain('apx.watchdog.action');
    expect(keys).toContain('apx.watchdog.policy_id');
  });

  it('default getActiveSpan returns null — no crash', async () => {
    const transport = makeRecordedTransport({
      tool_call: { action: 'reject', reason: 'denied' },
    });
    const guard = new WatchdogGuard({ client: new WatchdogClient({ transport }) });
    await expect(guard.forTool()('x', {})).rejects.toThrow('denied');
    // No span injected; nothing to assert other than no crash.
  });
});

// ---------------------------------------------------------------------------
// emitAgentMetadata
// ---------------------------------------------------------------------------

interface FakeTool extends ToolDescriptor {
  name: string;
  description?: string;
  ucName?: string;
  grants?: string[];
}

function makeFakeAgent(opts: {
  tools: FakeTool[];
  subAgents?: string[];
  instructions?: string;
}): { tools: FakeTool[]; subAgents: string[]; instructions: string } {
  return {
    tools: opts.tools,
    subAgents: opts.subAgents ?? [],
    instructions: opts.instructions ?? '',
  };
}

type FakeAgent = ReturnType<typeof makeFakeAgent>;
const fakeToolIter = (a: FakeAgent): Iterable<FakeTool & object> => a.tools;
const fakeSubIter = (a: FakeAgent): Iterable<string> => a.subAgents;

describe('emitAgentMetadata', () => {
  it('produces the full expected shape', () => {
    const classify: FakeTool = {
      name: 'classify_intent',
      description: 'Classify a customer query.',
      ucName: 'main.tools.classify_intent',
      grants: ['agent_consumers'],
    };
    // Attach a resource via the same WeakMap registry resources.ts uses.
    attachResources(classify as unknown as object, [
      makeResourceSpec('uc_function', 'main.tools.classify_intent'),
    ]);
    const score: FakeTool = { name: 'score', ucName: 'main.tools.score' };
    attachResources(score as unknown as object, [
      makeResourceSpec('uc_function', 'main.tools.score'),
    ]);
    const genie: FakeTool = { name: 'genie' };
    attachResources(genie as unknown as object, [makeResourceSpec('genie_space', 'space-abc')]);
    const vs: FakeTool = { name: 'vs' };
    attachResources(vs as unknown as object, [
      makeResourceSpec('vector_search_index', 'main.search.docs'),
    ]);

    const agent = makeFakeAgent({
      tools: [classify, score, genie, vs],
      subAgents: ['endpoints/billing'],
      instructions: 'You triage customer questions.',
    });

    const meta = emitAgentMetadata<FakeAgent>({
      agent,
      name: 'triage',
      model: 'databricks-claude-sonnet-4-6',
      toolIterator: fakeToolIter,
      subAgentIterator: fakeSubIter,
      instructions: agent.instructions,
    });

    expect(meta.name).toBe('triage');
    expect(meta.model).toBe('databricks-claude-sonnet-4-6');
    expect(meta.instructions).toBe('You triage customer questions.');

    const toolNames = new Set(meta.tools.map((t) => t.name));
    expect(toolNames.has('classify_intent')).toBe(true);
    const classifyMeta = meta.tools.find((t) => t.name === 'classify_intent')!;
    expect(classifyMeta.uc_name).toBe('main.tools.classify_intent');
    expect(classifyMeta.grants).toEqual(['agent_consumers']);

    expect(meta.sub_agents).toContain('endpoints/billing');

    const kinds = new Set(meta.resources.map((r) => r.kind));
    expect(kinds.has('uc_function')).toBe(true);
    expect(kinds.has('genie_space')).toBe(true);
    expect(kinds.has('vector_search_index')).toBe(true);
    expect(kinds.has('serving_endpoint')).toBe(true); // from model
  });

  it('minimal agent with no model returns null/empty fields', () => {
    const agent = makeFakeAgent({ tools: [] });
    const meta = emitAgentMetadata<FakeAgent>({
      agent,
      toolIterator: fakeToolIter,
      subAgentIterator: fakeSubIter,
    });
    expect(meta.name).toBeNull();
    expect(meta.model).toBeNull();
    expect(meta.tools).toEqual([]);
    expect(meta.resources).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// setUcTagsForAgent
// ---------------------------------------------------------------------------

function makeFullAgent(): FakeAgent {
  const classify: FakeTool = {
    name: 'classify_intent',
    description: 'Classify.',
    ucName: 'main.tools.classify_intent',
    grants: ['agent_consumers'],
  };
  attachResources(classify as unknown as object, [
    makeResourceSpec('uc_function', 'main.tools.classify_intent'),
  ]);
  return makeFakeAgent({
    tools: [classify],
    subAgents: ['endpoints/billing'],
    instructions: '...',
  });
}

describe('setUcTagsForAgent', () => {
  silenceWarn();

  it('writes the expected tag keys via mlflowClient.setRegisteredModelTag', async () => {
    const agent = makeFullAgent();
    const calls: Array<{ name: string; key: string; value: string }> = [];
    const mlflowClient: MlflowTagClient = {
      async setRegisteredModelTag(name, key, value) {
        calls.push({ name, key, value });
      },
    };

    const tags = await setUcTagsForAgent<FakeAgent>({
      registeredModelName: 'main.agents.triage',
      agent,
      model: 'databricks-claude-sonnet-4-6',
      name: 'triage',
      mlflowClient,
      toolIterator: fakeToolIter,
      subAgentIterator: fakeSubIter,
      instructions: agent.instructions,
    });

    const expected = new Set([
      'apx.agent.name',
      'apx.agent.model',
      'apx.agent.tool_count',
      'apx.agent.tools',
      'apx.agent.uc_functions',
      'apx.agent.sub_agents',
      'apx.agent.resources',
      'apx.agent.metadata',
    ]);
    for (const k of expected) expect(tags[k]).toBeDefined();

    expect(calls).toHaveLength(Object.keys(tags).length);
    const writtenKeys = new Set(calls.map((c) => c.key));
    expect(writtenKeys).toEqual(new Set(Object.keys(tags)));
  });

  it('propagates the model value into apx.agent.model', async () => {
    const agent = makeFullAgent();
    const mlflowClient: MlflowTagClient = { async setRegisteredModelTag() {} };
    const tags = await setUcTagsForAgent<FakeAgent>({
      registeredModelName: 'main.agents.triage',
      agent,
      model: 'databricks-claude-sonnet-4-6',
      mlflowClient,
      toolIterator: fakeToolIter,
      subAgentIterator: fakeSubIter,
    });
    expect(tags['apx.agent.model']).toBe('databricks-claude-sonnet-4-6');
  });

  it('continues past an individual tag-write failure', async () => {
    const agent = makeFullAgent();
    let n = 0;
    const calls: string[] = [];
    const mlflowClient: MlflowTagClient = {
      async setRegisteredModelTag(_name, key) {
        calls.push(key);
        n += 1;
        if (n === 2) throw new Error('fail');
      },
    };

    const tags = await setUcTagsForAgent<FakeAgent>({
      registeredModelName: 'main.agents.triage',
      agent,
      model: 'm',
      mlflowClient,
      toolIterator: fakeToolIter,
      subAgentIterator: fakeSubIter,
    });
    // Every tag write attempted even with a failure in the middle.
    expect(calls).toHaveLength(Object.keys(tags).length);
  });

  it('truncates long tag values to <= 4000 chars', async () => {
    // Build an agent with many long-named tools so the JSON payloads explode.
    const tools: FakeTool[] = [];
    const longName = 'x'.repeat(500);
    for (let i = 0; i < 20; i++) {
      const t: FakeTool = { name: `${longName}_${i}`, ucName: `main.tools.${longName}_${i}` };
      tools.push(t);
    }
    const agent = makeFakeAgent({ tools, instructions: 'y'.repeat(50_000) });
    const mlflowClient: MlflowTagClient = { async setRegisteredModelTag() {} };

    const tags = await setUcTagsForAgent<FakeAgent>({
      registeredModelName: 'main.agents.x',
      agent,
      mlflowClient,
      toolIterator: fakeToolIter,
      subAgentIterator: fakeSubIter,
      instructions: agent.instructions,
    });

    for (const v of Object.values(tags)) {
      expect(v.length).toBeLessThanOrEqual(4000);
    }
  });
});

// ---------------------------------------------------------------------------
// makeUcViolationWriter
// ---------------------------------------------------------------------------

describe('makeUcViolationWriter', () => {
  silenceWarn();

  it('rejects a non-three-part table path', () => {
    const sqlExecutor: SqlExecutor = async () => null;
    expect(() =>
      makeUcViolationWriter({ violationsTable: 'not.three', sqlExecutor }),
    ).toThrow(/three-part/);
  });

  it('inserts a row with decision + context fields inlined', async () => {
    const sqlCalls: string[] = [];
    const sqlExecutor: SqlExecutor = async (sql) => {
      sqlCalls.push(sql);
    };
    const writer = makeUcViolationWriter({
      violationsTable: 'main.x.violations',
      sqlExecutor,
      autoCreate: false,
    });
    await writer({
      type: 'violation_report',
      decision: {
        action: 'reject',
        reason: 'policy bad',
        policy_id: 'p-1',
        domain: 'security',
        metadata: { owner: 'data@x.com' },
      },
      context: { agent_name: 'triage', tool_name: 'leak_pii' },
    });
    expect(sqlCalls).toHaveLength(1);
    const sql = sqlCalls[0];
    expect(sql).toContain('INSERT INTO main.x.violations');
    expect(sql).toContain('reject');
    expect(sql).toContain('policy bad');
    expect(sql).toContain('p-1');
    expect(sql).toContain('security');
    expect(sql).toContain('triage');
  });

  it('passes through non-violation requests as {action: "allow"}', async () => {
    const sqlExecutor = vi.fn<SqlExecutor>(async () => null);
    const writer = makeUcViolationWriter({
      violationsTable: 'main.x.violations',
      sqlExecutor,
      autoCreate: false,
    });
    const out = await writer({ operation: 'tool_call', context: {} });
    expect(out).toEqual({ action: 'allow' });
    expect(sqlExecutor).not.toHaveBeenCalled();
  });

  it('auto-create runs once across multiple inserts', async () => {
    const sqlCalls: string[] = [];
    const sqlExecutor: SqlExecutor = async (sql) => {
      sqlCalls.push(sql);
    };
    const writer = makeUcViolationWriter({
      violationsTable: 'main.x.violations',
      sqlExecutor,
    });
    await writer({ type: 'violation_report', decision: { action: 'reject' }, context: {} });
    await writer({ type: 'violation_report', decision: { action: 'reject' }, context: {} });

    const createCalls = sqlCalls.filter((s) => s.includes('CREATE TABLE'));
    expect(createCalls).toHaveLength(1);
  });

  it("escapes single quotes in decision strings (' → '')", async () => {
    const sqlCalls: string[] = [];
    const sqlExecutor: SqlExecutor = async (sql) => {
      sqlCalls.push(sql);
    };
    const writer = makeUcViolationWriter({
      violationsTable: 'main.x.violations',
      sqlExecutor,
      autoCreate: false,
    });
    await writer({
      type: 'violation_report',
      decision: { action: 'reject', reason: "user's policy violated" },
      context: {},
    });
    expect(sqlCalls[0]).toContain("user''s policy violated");
  });

  it('logs-and-continues on SQL failure (returns {} instead of throwing)', async () => {
    const sqlExecutor: SqlExecutor = async () => {
      throw new Error('warehouse down');
    };
    const writer = makeUcViolationWriter({
      violationsTable: 'main.x.violations',
      sqlExecutor,
      autoCreate: false,
    });
    const result = await writer({
      type: 'violation_report',
      decision: { action: 'reject' },
      context: {},
    });
    expect(result).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// makeMcpTransport
// ---------------------------------------------------------------------------

describe('makeMcpTransport', () => {
  silenceWarn();

  it('calls mcpClient with (url, toolName, {operation, context})', async () => {
    const calls: Array<[string, string, Record<string, unknown>]> = [];
    const mcpClient: McpToolCallFn = async (url, name, args) => {
      calls.push([url, name, args]);
      return { action: 'reject', reason: 'blocked' };
    };
    const transport = makeMcpTransport({
      mcpUrl: 'https://watchdog.example.com/mcp',
      toolName: 'evaluate_operation',
      mcpClient,
    });
    const res = await transport({ operation: 'tool_call', context: { tool_name: 'leak_pii' } });
    expect(res).toEqual({ action: 'reject', reason: 'blocked' });
    expect(calls).toHaveLength(1);
    expect(calls[0][0]).toBe('https://watchdog.example.com/mcp');
    expect(calls[0][1]).toBe('evaluate_operation');
    expect(calls[0][2]).toEqual({ operation: 'tool_call', context: { tool_name: 'leak_pii' } });
  });

  it('falls back to allow on mcpClient error', async () => {
    const mcpClient: McpToolCallFn = async () => {
      throw new Error('mcp down');
    };
    const transport = makeMcpTransport({
      mcpUrl: 'https://watchdog.example.com/mcp',
      toolName: 'evaluate_operation',
      mcpClient,
    });
    const res = await transport({ operation: 'tool_call' });
    expect((res as Record<string, unknown>).action).toBe('allow');
    expect((res as Record<string, unknown>).reason).toContain('transport error');
  });

  it('passes through violation_report as {} (so it routes to the UC writer)', async () => {
    const mcpClient = vi.fn<McpToolCallFn>(async () => ({ action: 'allow' }));
    const transport = makeMcpTransport({
      mcpUrl: 'https://watchdog.example.com/mcp',
      toolName: 'evaluate_operation',
      mcpClient,
    });
    const res = await transport({ type: 'violation_report' });
    expect(res).toEqual({});
    expect(mcpClient).not.toHaveBeenCalled();
  });

  it('without mcpClient, evaluate falls back to allow', async () => {
    const transport = makeMcpTransport({
      mcpUrl: 'https://watchdog.example.com/mcp',
      toolName: 'evaluate_operation',
    });
    const res = await transport({ operation: 'tool_call' });
    expect((res as Record<string, unknown>).action).toBe('allow');
  });
});

// ---------------------------------------------------------------------------
// makeWatchdogTransport
// ---------------------------------------------------------------------------

describe('makeWatchdogTransport', () => {
  silenceWarn();

  it('routes violation_report to the UC writer (INSERT)', async () => {
    const sqlCalls: string[] = [];
    const sqlExecutor: SqlExecutor = async (sql) => {
      sqlCalls.push(sql);
    };
    const mcpClient: McpToolCallFn = async () => ({ action: 'allow' });
    const transport = makeWatchdogTransport({
      mcpUrl: 'https://watchdog.example.com/mcp',
      mcpToolName: 'evaluate_operation',
      violationsTable: 'main.x.violations',
      sqlExecutor,
      mcpClient,
    });
    await transport({
      type: 'violation_report',
      decision: { action: 'reject', reason: 'x' },
      context: { agent_name: 'triage' },
    });
    const inserts = sqlCalls.filter((s) => s.includes('INSERT INTO main.x.violations'));
    expect(inserts).toHaveLength(1);
  });

  it('routes evaluate to MCP', async () => {
    const mcpClient = vi.fn<McpToolCallFn>(async () => ({ action: 'reject', reason: 'blocked' }));
    const sqlExecutor: SqlExecutor = async () => null;
    const transport = makeWatchdogTransport({
      mcpUrl: 'https://watchdog.example.com/mcp',
      mcpToolName: 'evaluate_operation',
      violationsTable: 'main.x.violations',
      sqlExecutor,
      mcpClient,
    });
    const res = await transport({ operation: 'tool_call', context: {} });
    expect(res).toEqual({ action: 'reject', reason: 'blocked' });
    expect(mcpClient).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// End-to-end: WatchdogClient with the combined transport
// ---------------------------------------------------------------------------

describe('WatchdogClient + combined transport', () => {
  silenceWarn();

  it('reject decision via MCP triggers INSERT via reportViolation', async () => {
    const sqlCalls: string[] = [];
    const sqlExecutor: SqlExecutor = async (sql) => {
      sqlCalls.push(sql);
    };
    const mcpClient: McpToolCallFn = async () => ({
      action: 'reject',
      reason: 'blocked',
      policy_id: 'p-1',
    });
    const transport = makeWatchdogTransport({
      mcpUrl: 'https://watchdog.example.com/mcp',
      mcpToolName: 'evaluate_operation',
      violationsTable: 'main.x.violations',
      sqlExecutor,
      mcpClient,
    });
    const client = new WatchdogClient({ transport });
    const decision: WatchdogDecision = await client.evaluate({
      operation: 'tool_call',
      context: { tool_name: 'x' },
    });
    expect(decision.action).toBe('reject');
    expect(decision.policyId).toBe('p-1');
    await client.reportViolation(decision, { agent_name: 'triage', tool_name: 'x' });

    const inserts = sqlCalls.filter((s) => s.includes('INSERT INTO main.x.violations'));
    expect(inserts).toHaveLength(1);
    expect(inserts[0]).toContain('p-1');
    expect(inserts[0]).toContain('triage');
  });
});

// ---------------------------------------------------------------------------
// noOpTransport
// ---------------------------------------------------------------------------

describe('noOpTransport', () => {
  it('returns {action: "allow"}', async () => {
    expect(await noOpTransport({})).toEqual({ action: 'allow' });
  });
});
