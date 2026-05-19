/**
 * Tests for audit.ts — standard apx.* span-attribute schema.
 *
 * Mirrors python/tests/test_audit.py.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  AuditAttrs,
  hashForAudit,
  inputKeysSummary,
  outputSummary,
  setAuditAttrs,
  type SpanLike,
} from '../src/audit.js';

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

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
// AuditAttrs constants
// ---------------------------------------------------------------------------

describe('AuditAttrs constants', () => {
  it('every key lives under the apx. namespace', () => {
    for (const [name, value] of Object.entries(AuditAttrs)) {
      expect(typeof value).toBe('string');
      expect(value.startsWith('apx.')).toBe(true);
      // sanity: name is uppercase constant
      expect(name).toMatch(/^[A-Z_]+$/);
    }
  });

  it.each([
    ['AGENT_NAME', 'apx.agent.name'],
    ['SESSION_ID', 'apx.session.id'],
    ['OPERATION', 'apx.operation'],
    ['TOOL_NAME', 'apx.tool.name'],
    ['TOOL_UC_FUNCTION', 'apx.tool.uc_function'],
    ['MODEL_ENDPOINT', 'apx.model.endpoint'],
    ['MODEL_INPUT_TOKENS', 'apx.model.input_tokens'],
    ['WATCHDOG_ACTION', 'apx.watchdog.action'],
    ['WATCHDOG_POLICY_ID', 'apx.watchdog.policy_id'],
    ['USER_TOKEN_PROVIDED', 'apx.user.token_provided'],
  ] as const)('AuditAttrs.%s === %s', (name, expected) => {
    expect((AuditAttrs as Record<string, string>)[name]).toBe(expected);
  });
});

// ---------------------------------------------------------------------------
// setAuditAttrs
// ---------------------------------------------------------------------------

describe('setAuditAttrs', () => {
  it('maps short kwargs to canonical keys', () => {
    const span = mockSpan();
    setAuditAttrs(span, {
      agentName: 'triage',
      toolName: 'classify_intent',
      watchdogAction: 'reject',
    });
    const keys = span.calls.map(([k]) => k);
    expect(keys).toContain('apx.agent.name');
    expect(keys).toContain('apx.tool.name');
    expect(keys).toContain('apx.watchdog.action');
  });

  it('skips null, undefined, and empty-string values', () => {
    const span = mockSpan();
    setAuditAttrs(span, {
      agentName: 'triage',
      toolName: null,
      watchdogReason: '',
      watchdogPolicyId: 'p-1',
      sessionId: undefined,
    });
    const keys = span.calls.map(([k]) => k);
    expect(keys).toContain('apx.agent.name');
    expect(keys).toContain('apx.watchdog.policy_id');
    expect(keys).not.toContain('apx.tool.name');
    expect(keys).not.toContain('apx.watchdog.reason');
    expect(keys).not.toContain('apx.session.id');
  });

  it('is a no-op for a null span', () => {
    expect(() =>
      setAuditAttrs(null, { agentName: 'triage', toolName: 'x' }),
    ).not.toThrow();
  });

  it('is a no-op for an undefined span', () => {
    expect(() =>
      setAuditAttrs(undefined, { agentName: 'triage', toolName: 'x' }),
    ).not.toThrow();
  });

  it('throws on an unknown kwarg (typo failure)', () => {
    const span = mockSpan();
    expect(() => setAuditAttrs(span, { madeUpField: 'value' })).toThrow(
      /unknown kwarg/,
    );
  });

  it('swallows span.setAttribute exceptions', () => {
    const span: SpanLike = {
      setAttribute: vi.fn(() => {
        throw new Error('span broken');
      }),
    };
    expect(() => setAuditAttrs(span, { agentName: 'triage' })).not.toThrow();
    expect(span.setAttribute).toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// hashForAudit
// ---------------------------------------------------------------------------

describe('hashForAudit', () => {
  it('is deterministic for identical inputs', () => {
    expect(hashForAudit('hello')).toBe(hashForAudit('hello'));
    expect(hashForAudit({ a: 1 })).toBe(hashForAudit({ a: 1 }));
  });

  it('differs across distinct inputs', () => {
    expect(hashForAudit('a')).not.toBe(hashForAudit('b'));
  });

  it('respects the length parameter', () => {
    expect(hashForAudit('x', 8)).toHaveLength(8);
    expect(hashForAudit('x', 32)).toHaveLength(32);
  });

  it('uses lowercase hex only', () => {
    const h = hashForAudit({ complex: ['nested', 1] });
    expect(h).toMatch(/^[0-9a-f]+$/);
  });

  it('defaults to length 16', () => {
    expect(hashForAudit('x')).toHaveLength(16);
  });
});

// ---------------------------------------------------------------------------
// outputSummary
// ---------------------------------------------------------------------------

describe('outputSummary', () => {
  it('handles strings', () => {
    const { typeName, sizeEstimate } = outputSummary('hello world');
    expect(typeName).toBe('str');
    expect(sizeEstimate).toBe(11);
  });

  it('handles arrays', () => {
    const { typeName, sizeEstimate } = outputSummary([1, 2, 3]);
    expect(typeName).toBe('list');
    expect(sizeEstimate).toBe(3);
  });

  it('handles plain objects (key count)', () => {
    const { typeName, sizeEstimate } = outputSummary({ a: 1, b: 2 });
    expect(typeName).toBe('dict');
    expect(sizeEstimate).toBe(2);
  });

  it('falls back to String(value).length for primitive numbers', () => {
    const { typeName, sizeEstimate } = outputSummary(42);
    expect(typeName).toBe('int');
    expect(sizeEstimate).toBe(2); // "42"
  });
});

// ---------------------------------------------------------------------------
// inputKeysSummary
// ---------------------------------------------------------------------------

describe('inputKeysSummary', () => {
  it('returns sorted, comma-separated dict keys', () => {
    expect(inputKeysSummary({ b: 1, a: 2 })).toBe('a,b');
  });

  it('returns "<string>" for strings', () => {
    expect(inputKeysSummary('hello')).toBe('<string>');
  });

  it('returns the type name for other inputs', () => {
    expect(inputKeysSummary([1, 2, 3])).toBe('list');
  });

  it('returns an empty string for an empty dict', () => {
    expect(inputKeysSummary({})).toBe('');
  });
});
