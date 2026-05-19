/**
 * Tests for mlflow-tracing.ts — pluggable MLflow-style tracing helpers.
 *
 * Mirrors python/tests/test_mlflow_tracing.py to the extent the TS API
 * differs (no first-party MLflow JS SDK — everything is injectable).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  currentActiveSpan,
  enableLangchainAutolog,
  isMlflowAvailable,
  safeSpan,
  setActiveSpanResolver,
  setMlflowAvailabilityCheck,
  setMlflowStartSpan,
  setSpanAttribute,
  setSpanOutputs,
  withSafeSpan,
  type SpanLike,
} from '../src/mlflow-tracing.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeMockSpan(): SpanLike & {
  attrs: Array<[string, unknown]>;
  outputs: unknown[];
  inputs: unknown[];
  ended: number;
} {
  const attrs: Array<[string, unknown]> = [];
  const outputs: unknown[] = [];
  const inputs: unknown[] = [];
  let ended = 0;
  return {
    attrs,
    outputs,
    inputs,
    get ended() {
      return ended;
    },
    setAttribute(key, value) {
      attrs.push([key, value]);
    },
    setOutputs(o) {
      outputs.push(o);
    },
    setInputs(i) {
      inputs.push(i);
    },
    end() {
      ended++;
    },
  };
}

// ---------------------------------------------------------------------------
// Reset module state between tests
// ---------------------------------------------------------------------------

beforeEach(() => {
  setMlflowStartSpan(null);
  setActiveSpanResolver(null);
  setMlflowAvailabilityCheck(null);
});

afterEach(() => {
  setMlflowStartSpan(null);
  setActiveSpanResolver(null);
  setMlflowAvailabilityCheck(null);
});

// ---------------------------------------------------------------------------
// isMlflowAvailable
// ---------------------------------------------------------------------------

describe('isMlflowAvailable', () => {
  it('returns false by default', () => {
    expect(isMlflowAvailable()).toBe(false);
  });

  it('setMlflowAvailabilityCheck wires a custom check', () => {
    setMlflowAvailabilityCheck(() => true);
    expect(isMlflowAvailable()).toBe(true);
    setMlflowAvailabilityCheck(() => false);
    expect(isMlflowAvailable()).toBe(false);
  });

  it('returns false when the check throws', () => {
    setMlflowAvailabilityCheck(() => {
      throw new Error('boom');
    });
    expect(isMlflowAvailable()).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// enableLangchainAutolog
// ---------------------------------------------------------------------------

describe('enableLangchainAutolog', () => {
  it('returns false (no LangChain JS autolog)', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(enableLangchainAutolog()).toBe(false);
    warn.mockRestore();
  });
});

// ---------------------------------------------------------------------------
// safeSpan
// ---------------------------------------------------------------------------

describe('safeSpan', () => {
  it('returns a no-op span when no factory is registered', () => {
    const span = safeSpan('test');
    expect(span).toBeDefined();
    // All methods exist and are safe to call.
    expect(() => span.setAttribute('k', 'v')).not.toThrow();
    expect(() => span.setOutputs?.({ a: 1 })).not.toThrow();
    expect(() => span.end?.()).not.toThrow();
  });

  it('returns a span produced by the registered factory', () => {
    const mock = makeMockSpan();
    setMlflowStartSpan(() => mock);
    const span = safeSpan('test', { spanType: 'AGENT' });
    expect(span).toBe(mock);
  });

  it('records inputs + attributes during construction', () => {
    const mock = makeMockSpan();
    setMlflowStartSpan(() => mock);
    safeSpan('test', {
      inputs: { hello: 'world' },
      attributes: { 'apx.foo': 'bar', 'apx.skip_me': null, 'apx.also_skip': undefined },
    });
    expect(mock.inputs).toEqual([{ hello: 'world' }]);
    // null/undefined attributes are filtered.
    expect(mock.attrs).toEqual([['apx.foo', 'bar']]);
  });

  it('falls back to no-op when the factory throws', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    setMlflowStartSpan(() => {
      throw new Error('tracer dead');
    });
    const span = safeSpan('test');
    expect(span).toBeDefined();
    // No-op span — calling its methods doesn't throw.
    expect(() => span.setAttribute('k', 'v')).not.toThrow();
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it('falls back to no-op when the factory returns null', () => {
    setMlflowStartSpan(() => null);
    const span = safeSpan('test');
    expect(() => span.setAttribute('k', 'v')).not.toThrow();
  });

  it('does not throw when span.setAttribute throws during setup', () => {
    const broken: SpanLike = {
      setAttribute() {
        throw new Error('broken attr');
      },
    };
    setMlflowStartSpan(() => broken);
    expect(() => safeSpan('t', { attributes: { foo: 'bar' } })).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// withSafeSpan
// ---------------------------------------------------------------------------

describe('withSafeSpan', () => {
  it('runs the callback and returns its result', async () => {
    const result = await withSafeSpan('t', async () => 42);
    expect(result).toBe(42);
  });

  it('ends the span after the callback completes', async () => {
    const mock = makeMockSpan();
    setMlflowStartSpan(() => mock);
    await withSafeSpan('t', async () => 'ok');
    expect(mock.ended).toBe(1);
  });

  it('ends the span even when the callback throws', async () => {
    const mock = makeMockSpan();
    setMlflowStartSpan(() => mock);
    await expect(
      withSafeSpan('t', async () => {
        throw new Error('boom');
      }),
    ).rejects.toThrow('boom');
    expect(mock.ended).toBe(1);
  });

  it('passes the span into the callback', async () => {
    const mock = makeMockSpan();
    setMlflowStartSpan(() => mock);
    await withSafeSpan('t', (span) => {
      span.setAttribute('inside', true);
    });
    expect(mock.attrs).toContainEqual(['inside', true]);
  });

  it('does not throw if span.end throws', async () => {
    const broken: SpanLike = {
      setAttribute() {},
      end() {
        throw new Error('end blew up');
      },
    };
    setMlflowStartSpan(() => broken);
    await expect(withSafeSpan('t', () => 'ok')).resolves.toBe('ok');
  });
});

// ---------------------------------------------------------------------------
// setSpanOutputs
// ---------------------------------------------------------------------------

describe('setSpanOutputs', () => {
  it('no-ops on null', () => {
    expect(() => setSpanOutputs(null, { a: 1 })).not.toThrow();
  });

  it('no-ops on undefined', () => {
    expect(() => setSpanOutputs(undefined, { a: 1 })).not.toThrow();
  });

  it('forwards to span.setOutputs', () => {
    const mock = makeMockSpan();
    setSpanOutputs(mock, { ok: true });
    expect(mock.outputs).toEqual([{ ok: true }]);
  });

  it('swallows errors from a broken span', () => {
    const broken: SpanLike = {
      setAttribute() {},
      setOutputs() {
        throw new Error('nope');
      },
    };
    expect(() => setSpanOutputs(broken, { a: 1 })).not.toThrow();
  });

  it('no-ops on a span without setOutputs', () => {
    const minimal: SpanLike = { setAttribute() {} };
    expect(() => setSpanOutputs(minimal, { a: 1 })).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// setSpanAttribute
// ---------------------------------------------------------------------------

describe('setSpanAttribute', () => {
  it('no-ops on null', () => {
    expect(() => setSpanAttribute(null, 'k', 'v')).not.toThrow();
  });

  it('forwards to span.setAttribute', () => {
    const mock = makeMockSpan();
    setSpanAttribute(mock, 'apx.foo', 'bar');
    expect(mock.attrs).toEqual([['apx.foo', 'bar']]);
  });

  it('swallows errors from a broken span', () => {
    const broken: SpanLike = {
      setAttribute() {
        throw new Error('nope');
      },
    };
    expect(() => setSpanAttribute(broken, 'k', 'v')).not.toThrow();
  });

  it('skips null / undefined values', () => {
    const mock = makeMockSpan();
    setSpanAttribute(mock, 'k', null);
    setSpanAttribute(mock, 'k', undefined);
    expect(mock.attrs).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// currentActiveSpan
// ---------------------------------------------------------------------------

describe('currentActiveSpan', () => {
  it('returns null by default', () => {
    expect(currentActiveSpan()).toBeNull();
  });

  it('returns the span the resolver yields', () => {
    const mock = makeMockSpan();
    setActiveSpanResolver(() => mock);
    expect(currentActiveSpan()).toBe(mock);
  });

  it('returns null if the resolver throws', () => {
    setActiveSpanResolver(() => {
      throw new Error('dead');
    });
    expect(currentActiveSpan()).toBeNull();
  });

  it('reverts to default null on setActiveSpanResolver(null)', () => {
    setActiveSpanResolver(() => makeMockSpan());
    setActiveSpanResolver(null);
    expect(currentActiveSpan()).toBeNull();
  });
});
