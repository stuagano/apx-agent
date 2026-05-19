/**
 * Tests for `lint.ts` — static checks against agent definitions.
 *
 * Mirrors python/tests/test_lint.py rule-by-rule (L001-L004) plus the
 * public `lintAgent` entry point. Findings are sorted for stable diffs.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';

import { lintAgent, Severity, type LintFinding, type LintableAgent } from '../src/lint.js';

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

interface AgentInput {
  name?: string;
  instructions?: string;
  tools?: Array<{ name?: string; description?: string }>;
  subAgents?: string[];
  model?: string;
}

function agent(input: AgentInput): AgentInput {
  return input;
}

// ---------------------------------------------------------------------------
// L001 — missing instructions
// ---------------------------------------------------------------------------

describe('L001 — missing instructions', () => {
  it('fires on empty instructions', () => {
    const findings = lintAgent(agent({ instructions: '' }));
    expect(findings.some((f) => f.rule === 'L001')).toBe(true);
    const l001 = findings.find((f) => f.rule === 'L001')!;
    expect(l001.severity).toBe(Severity.Error);
    expect(l001.message.toLowerCase()).toContain('no instructions');
  });

  it('treats whitespace-only as empty', () => {
    const findings = lintAgent(agent({ instructions: '   \n  \t ' }));
    expect(findings.some((f) => f.rule === 'L001')).toBe(true);
  });

  it('does not fire when instructions are present', () => {
    const findings = lintAgent(agent({ instructions: 'You are a helpful assistant.' }));
    expect(findings.some((f) => f.rule === 'L001')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// L002 — tool descriptions
// ---------------------------------------------------------------------------

describe('L002 — tools missing descriptions', () => {
  it('fires on a tool with no description', () => {
    const findings = lintAgent(
      agent({
        instructions: 'x',
        tools: [{ name: 'undocumented_tool' }],
      }),
    );
    expect(findings.some((f) => f.rule === 'L002')).toBe(true);
    const l002 = findings.find((f) => f.rule === 'L002')!;
    expect(l002.severity).toBe(Severity.Warning);
    expect(l002.toolName).toBe('undocumented_tool');
  });

  it('fires on whitespace-only description', () => {
    const findings = lintAgent(
      agent({
        instructions: 'x',
        tools: [{ name: 'sloppy', description: '   ' }],
      }),
    );
    expect(findings.some((f) => f.rule === 'L002')).toBe(true);
  });

  it('does not fire when description is present', () => {
    const findings = lintAgent(
      agent({
        instructions: 'x',
        tools: [{ name: 'square', description: 'Square a number.' }],
      }),
    );
    expect(findings.some((f) => f.rule === 'L002')).toBe(false);
  });

  it('finds only the undocumented tool when mixed', () => {
    const findings = lintAgent(
      agent({
        instructions: 'x',
        tools: [
          { name: 'square', description: 'Square a number.' },
          { name: 'undocumented_tool' },
        ],
      }),
    );
    const l002s = findings.filter((f) => f.rule === 'L002');
    expect(l002s).toHaveLength(1);
    expect(l002s[0]!.toolName).toBe('undocumented_tool');
  });
});

// ---------------------------------------------------------------------------
// L003 — sub-agent URLs with unset env vars
// ---------------------------------------------------------------------------

describe('L003 — sub-agent URL env-var refs', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('fires on $UNSET_VAR', () => {
    const findings = lintAgent(
      agent({
        instructions: 'x',
        subAgents: ['$AGENT_THAT_DOES_NOT_EXIST_xyz123/api'],
      }),
    );
    const l003 = findings.find((f) => f.rule === 'L003');
    expect(l003).toBeDefined();
    expect(l003!.severity).toBe(Severity.Error);
    expect(l003!.message).toContain('AGENT_THAT_DOES_NOT_EXIST_xyz123');
  });

  it('does not fire when env var is set', () => {
    vi.stubEnv('APX_LINT_TEST_VAR', 'https://example.com');
    const findings = lintAgent(
      agent({
        instructions: 'x',
        subAgents: ['$APX_LINT_TEST_VAR/api'],
      }),
    );
    expect(findings.some((f) => f.rule === 'L003')).toBe(false);
  });

  it('does not fire on literal URLs without env vars', () => {
    const findings = lintAgent(
      agent({
        instructions: 'x',
        subAgents: ['https://my-agent.databricksapps.com/api'],
      }),
    );
    expect(findings.some((f) => f.rule === 'L003')).toBe(false);
  });

  it('handles ${VAR} curly-brace form', () => {
    const findings = lintAgent(
      agent({
        instructions: 'x',
        subAgents: ['${UNSET_LINT_VAR_curly}/api'],
      }),
    );
    const l003 = findings.find((f) => f.rule === 'L003');
    expect(l003).toBeDefined();
    expect(l003!.message).toContain('UNSET_LINT_VAR_curly');
  });

  it('treats empty-string env var as unset', () => {
    vi.stubEnv('APX_LINT_TEST_VAR', '');
    const findings = lintAgent(
      agent({
        instructions: 'x',
        subAgents: ['$APX_LINT_TEST_VAR/api'],
      }),
    );
    expect(findings.some((f) => f.rule === 'L003')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// L004 — model name shape
// ---------------------------------------------------------------------------

describe('L004 — unrecognized model name', () => {
  it('does not fire on databricks-claude-sonnet-4-6', () => {
    const findings = lintAgent(agent({ instructions: 'x' }), {
      model: 'databricks-claude-sonnet-4-6',
    });
    expect(findings.some((f) => f.rule === 'L004')).toBe(false);
  });

  it('does not fire on databricks-claude-opus-4-7', () => {
    const findings = lintAgent(agent({ instructions: 'x' }), {
      model: 'databricks-claude-opus-4-7',
    });
    expect(findings.some((f) => f.rule === 'L004')).toBe(false);
  });

  it('does not fire on endpoints/billing-agent', () => {
    const findings = lintAgent(agent({ instructions: 'x' }), {
      model: 'endpoints/billing-agent',
    });
    expect(findings.some((f) => f.rule === 'L004')).toBe(false);
  });

  it('fires on gpt-4o-2024-08-06', () => {
    const findings = lintAgent(agent({ instructions: 'x' }), { model: 'gpt-4o-2024-08-06' });
    const l004 = findings.find((f) => f.rule === 'L004');
    expect(l004).toBeDefined();
    expect(l004!.severity).toBe(Severity.Warning);
    expect(l004!.message).toContain('gpt-4o-2024-08-06');
  });

  it('deduplicates same model across kwarg + agent attribute', () => {
    const findings = lintAgent(
      agent({ instructions: 'x', model: 'claude-3-5-sonnet-20241022' }),
      { model: 'claude-3-5-sonnet-20241022' },
    );
    expect(findings.filter((f) => f.rule === 'L004')).toHaveLength(1);
  });

  it('does not fire when no model is provided anywhere', () => {
    const findings = lintAgent(agent({ instructions: 'x' }));
    expect(findings.some((f) => f.rule === 'L004')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// lintAgent — public entry point
// ---------------------------------------------------------------------------

describe('lintAgent — public entry point', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('returns empty list for a clean agent', () => {
    const findings = lintAgent(
      agent({
        instructions: 'You are a helpful assistant.',
        tools: [{ name: 'square', description: 'Square a number.' }],
      }),
      { model: 'databricks-claude-sonnet-4-6' },
    );
    expect(findings).toEqual([]);
  });

  it('aggregates findings for all four rules', () => {
    const findings = lintAgent(
      agent({
        instructions: '',
        tools: [{ name: 'undocumented_tool' }],
        subAgents: ['$UNSET_LINT_ENV_abc/api'],
      }),
      { model: 'gpt-4o' },
    );
    const codes = new Set(findings.map((f) => f.rule));
    expect(codes).toEqual(new Set(['L001', 'L002', 'L003', 'L004']));
  });

  it('returns findings sorted by rule code', () => {
    const findings = lintAgent(
      agent({
        instructions: '',
        tools: [{ name: 'undocumented_tool' }],
        subAgents: ['$UNSET_LINT_ENV_xxx/api'],
      }),
      { model: 'gpt-4o' },
    );
    const codes = findings.map((f) => f.rule);
    expect(codes).toEqual([...codes].sort());
  });

  it('accepts a createAgentPlugin-shaped value via exports()', () => {
    const plugin = {
      exports: () => ({
        getConfig: () => ({
          name: 'wrapped',
          instructions: '', // L001 should fire
          tools: [],
          subAgents: [],
        }),
        getTools: () => [],
      }),
    };
    const findings = lintAgent(plugin);
    const l001 = findings.find((f) => f.rule === 'L001');
    expect(l001).toBeDefined();
    expect(l001!.agentName).toBe('wrapped');
  });

  it('accepts a bare AgentExports value via getConfig()', () => {
    const exports = {
      getConfig: () => ({
        name: 'bare-exports',
        instructions: '',
      }),
    };
    const findings = lintAgent(exports);
    expect(findings.some((f) => f.rule === 'L001' && f.agentName === 'bare-exports')).toBe(true);
  });

  it('supports a custom agentIterator for composite agents', () => {
    const tree = {
      children: [
        agent({ name: 'good', instructions: 'do work' }),
        agent({ name: 'bad', instructions: '' }),
      ],
    };
    const walker = (root: unknown): Iterable<unknown> => {
      const node = root as { children: LintableAgent[] };
      return node.children;
    };
    const findings = lintAgent(tree, { agentIterator: walker });
    const l001s = findings.filter((f) => f.rule === 'L001');
    expect(l001s).toHaveLength(1);
    expect(l001s[0]!.agentName).toBe('bad');
  });

  it('preserves agentName for composite findings when set', () => {
    const tree = {
      children: [
        agent({ name: 'leaf-a', instructions: 'ok', tools: [{ name: 'tool-x' }] }),
      ],
    };
    const walker = (root: unknown): Iterable<unknown> => {
      const node = root as { children: LintableAgent[] };
      return node.children;
    };
    const findings = lintAgent(tree, { agentIterator: walker });
    const l002 = findings.find((f) => f.rule === 'L002');
    expect(l002).toBeDefined();
    expect(l002!.agentName).toBe('leaf-a');
    expect(l002!.toolName).toBe('tool-x');
  });
});

// ---------------------------------------------------------------------------
// LintFinding shape sanity
// ---------------------------------------------------------------------------

describe('LintFinding shape', () => {
  it('is well-typed', () => {
    const f: LintFinding = {
      rule: 'L001',
      severity: Severity.Error,
      message: 'oops',
    };
    expect(f.rule).toBe('L001');
    expect(f.severity).toBe('error');
  });
});
