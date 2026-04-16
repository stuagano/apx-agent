/**
 * Tests for AgentState — shared key-value store for workflow agents.
 */

import { describe, it, expect } from 'vitest';
import { AgentState } from '../src/workflows/state.js';

describe('AgentState', () => {
  // -------------------------------------------------------------------------
  // Construction
  // -------------------------------------------------------------------------

  it('initializes empty with no arguments', () => {
    const state = new AgentState();
    expect(state.keys()).toHaveLength(0);
    expect(state.toObject()).toEqual({});
  });

  it('initializes with seed values', () => {
    const state = new AgentState({ topic: 'billing', count: 42 });
    expect(state.get('topic')).toBe('billing');
    expect(state.get('count')).toBe(42);
  });

  it('accepts falsy seed values (null, 0, false, empty string)', () => {
    const state = new AgentState({ zero: 0, nope: false, empty: '', nil: null });
    expect(state.get('zero')).toBe(0);
    expect(state.get('nope')).toBe(false);
    expect(state.get('empty')).toBe('');
    expect(state.get('nil')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // get
  // -------------------------------------------------------------------------

  it('get returns undefined for missing keys', () => {
    const state = new AgentState();
    expect(state.get('nope')).toBeUndefined();
  });

  it('get is generic and can be typed', () => {
    const state = new AgentState({ rows: [1, 2, 3] });
    const rows = state.get<number[]>('rows');
    expect(rows).toEqual([1, 2, 3]);
  });

  // -------------------------------------------------------------------------
  // set
  // -------------------------------------------------------------------------

  it('set stores a value retrievable via get', () => {
    const state = new AgentState();
    state.set('answer', 42);
    expect(state.get('answer')).toBe(42);
  });

  it('set overwrites existing value', () => {
    const state = new AgentState({ key: 'old' });
    state.set('key', 'new');
    expect(state.get('key')).toBe('new');
  });

  it('set can store objects and arrays', () => {
    const state = new AgentState();
    const obj = { a: 1, b: [2, 3] };
    state.set('data', obj);
    expect(state.get('data')).toEqual(obj);
  });

  // -------------------------------------------------------------------------
  // has
  // -------------------------------------------------------------------------

  it('has returns true for existing key', () => {
    const state = new AgentState({ x: 1 });
    expect(state.has('x')).toBe(true);
  });

  it('has returns false for missing key', () => {
    const state = new AgentState();
    expect(state.has('missing')).toBe(false);
  });

  it('has returns true even for falsy values', () => {
    const state = new AgentState({ zero: 0, nope: false, empty: '' });
    expect(state.has('zero')).toBe(true);
    expect(state.has('nope')).toBe(true);
    expect(state.has('empty')).toBe(true);
  });

  // -------------------------------------------------------------------------
  // delete
  // -------------------------------------------------------------------------

  it('delete removes an existing key', () => {
    const state = new AgentState({ a: 1 });
    const deleted = state.delete('a');
    expect(deleted).toBe(true);
    expect(state.has('a')).toBe(false);
  });

  it('delete returns false for missing key', () => {
    const state = new AgentState();
    expect(state.delete('ghost')).toBe(false);
  });

  it('delete does not affect other keys', () => {
    const state = new AgentState({ a: 1, b: 2 });
    state.delete('a');
    expect(state.has('b')).toBe(true);
    expect(state.get('b')).toBe(2);
  });

  // -------------------------------------------------------------------------
  // keys / toObject
  // -------------------------------------------------------------------------

  it('keys returns all keys', () => {
    const state = new AgentState({ a: 1, b: 2, c: 3 });
    expect(state.keys().sort()).toEqual(['a', 'b', 'c']);
  });

  it('keys returns empty array when store is empty', () => {
    expect(new AgentState().keys()).toEqual([]);
  });

  it('toObject returns all entries as a plain object', () => {
    const state = new AgentState({ x: 1, y: 'two' });
    state.set('z', true);
    expect(state.toObject()).toEqual({ x: 1, y: 'two', z: true });
  });

  it('toObject returns an empty object for empty state', () => {
    expect(new AgentState().toObject()).toEqual({});
  });

  // -------------------------------------------------------------------------
  // interpolate
  // -------------------------------------------------------------------------

  it('interpolate replaces a single placeholder', () => {
    const state = new AgentState({ topic: 'billing' });
    expect(state.interpolate('Analyze {topic}')).toBe('Analyze billing');
  });

  it('interpolate replaces multiple placeholders', () => {
    const state = new AgentState({ topic: 'billing', user: 'Alice' });
    const result = state.interpolate('Handle {topic} for {user}.');
    expect(result).toBe('Handle billing for Alice.');
  });

  it('interpolate leaves unknown placeholders intact', () => {
    const state = new AgentState({ known: 'yes' });
    const result = state.interpolate('Hello {known} and {unknown}');
    expect(result).toBe('Hello yes and {unknown}');
  });

  it('interpolate coerces values to strings', () => {
    const state = new AgentState({ count: 42, flag: true });
    expect(state.interpolate('count={count} flag={flag}')).toBe('count=42 flag=true');
  });

  it('interpolate returns the template unchanged if no placeholders', () => {
    const state = new AgentState({ a: 1 });
    const tmpl = 'No placeholders here.';
    expect(state.interpolate(tmpl)).toBe(tmpl);
  });

  it('interpolate handles temp:-prefixed keys in placeholders', () => {
    const state = new AgentState();
    state.set('temp:scratch', 'scratch-value');
    const result = state.interpolate('value={temp:scratch}');
    expect(result).toBe('value=scratch-value');
  });

  it('interpolate does not replace empty placeholder {}', () => {
    const state = new AgentState({ '': 'oops' });
    // {} is not matched by \w+ pattern — leave as-is
    const result = state.interpolate('before {} after');
    expect(result).toBe('before {} after');
  });

  // -------------------------------------------------------------------------
  // clearTemp
  // -------------------------------------------------------------------------

  it('clearTemp removes all temp:-prefixed keys', () => {
    const state = new AgentState();
    state.set('persist', 'stay');
    state.set('temp:scratch', 'gone');
    state.set('temp:work', 'also gone');
    state.clearTemp();

    expect(state.has('persist')).toBe(true);
    expect(state.has('temp:scratch')).toBe(false);
    expect(state.has('temp:work')).toBe(false);
  });

  it('clearTemp does not remove non-temp keys', () => {
    const state = new AgentState({ a: 1, b: 2 });
    state.clearTemp();
    expect(state.has('a')).toBe(true);
    expect(state.has('b')).toBe(true);
  });

  it('clearTemp is a no-op on empty state', () => {
    const state = new AgentState();
    expect(() => state.clearTemp()).not.toThrow();
    expect(state.keys()).toHaveLength(0);
  });

  it('clearTemp only removes keys that start with temp:', () => {
    const state = new AgentState();
    state.set('temporary', 'keep');   // does NOT start with "temp:"
    state.set('temp:x', 'remove');
    state.clearTemp();

    expect(state.has('temporary')).toBe(true);
    expect(state.has('temp:x')).toBe(false);
  });

  // -------------------------------------------------------------------------
  // clone
  // -------------------------------------------------------------------------

  it('clone creates an independent copy', () => {
    const original = new AgentState({ a: 1 });
    const copy = original.clone();

    copy.set('a', 99);
    expect(original.get('a')).toBe(1);   // original unchanged
    expect(copy.get('a')).toBe(99);
  });

  it('clone copies all current keys', () => {
    const original = new AgentState({ x: 'hello', y: 42 });
    const copy = original.clone();
    expect(copy.get('x')).toBe('hello');
    expect(copy.get('y')).toBe(42);
  });

  it('clone: adding keys to copy does not affect original', () => {
    const original = new AgentState({ a: 1 });
    const copy = original.clone();
    copy.set('new', 'value');

    expect(original.has('new')).toBe(false);
  });

  it('clone: adding keys to original does not affect copy', () => {
    const original = new AgentState({ a: 1 });
    const copy = original.clone();
    original.set('extra', 'extra');

    expect(copy.has('extra')).toBe(false);
  });

  it('clone of empty state produces empty state', () => {
    const copy = new AgentState().clone();
    expect(copy.keys()).toHaveLength(0);
  });
});
