/**
 * Tests for Session and InMemorySessionStore.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import {
  Session,
  InMemorySessionStore,
  setDefaultSessionStore,
  getDefaultSessionStore,
} from '../src/workflows/session.js';
import { AgentState } from '../src/workflows/state.js';
import type { SessionStore, SessionSnapshot } from '../src/workflows/session.js';

// ---------------------------------------------------------------------------
// InMemorySessionStore
// ---------------------------------------------------------------------------

describe('InMemorySessionStore', () => {
  let store: InMemorySessionStore;

  beforeEach(() => {
    store = new InMemorySessionStore();
  });

  const makeSnapshot = (id: string, overrides?: Partial<SessionSnapshot>): SessionSnapshot => ({
    id,
    messages: [],
    state: {},
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    ...overrides,
  });

  it('save and load round-trip', async () => {
    const snap = makeSnapshot('s1', { state: { key: 'value' } });
    await store.save('s1', snap);
    const loaded = await store.load('s1');
    expect(loaded).toEqual(snap);
  });

  it('load returns null for unknown id', async () => {
    expect(await store.load('ghost')).toBeNull();
  });

  it('save overwrites existing snapshot', async () => {
    await store.save('s1', makeSnapshot('s1', { state: { v: 1 } }));
    await store.save('s1', makeSnapshot('s1', { state: { v: 2 } }));
    const loaded = await store.load('s1');
    expect(loaded?.state).toEqual({ v: 2 });
  });

  it('save stores a clone (mutations after save do not affect stored data)', async () => {
    const snap = makeSnapshot('s1', { state: { count: 1 } });
    await store.save('s1', snap);
    snap.state['count'] = 99;  // mutate after save

    const loaded = await store.load('s1');
    expect(loaded?.state['count']).toBe(1);
  });

  it('load returns a clone (mutations do not affect stored data)', async () => {
    await store.save('s1', makeSnapshot('s1', { state: { count: 1 } }));
    const loaded = await store.load('s1');
    loaded!.state['count'] = 99;

    const reloaded = await store.load('s1');
    expect(reloaded?.state['count']).toBe(1);
  });

  it('delete removes the session', async () => {
    await store.save('s1', makeSnapshot('s1'));
    await store.delete('s1');
    expect(await store.load('s1')).toBeNull();
  });

  it('delete is a no-op for unknown id', async () => {
    await expect(store.delete('ghost')).resolves.toBeUndefined();
  });

  it('list returns all saved session ids', async () => {
    await store.save('a', makeSnapshot('a'));
    await store.save('b', makeSnapshot('b'));
    await store.save('c', makeSnapshot('c'));
    const ids = await store.list();
    expect(ids.sort()).toEqual(['a', 'b', 'c']);
  });

  it('list returns empty array when store is empty', async () => {
    expect(await store.list()).toEqual([]);
  });

  it('list does not include deleted sessions', async () => {
    await store.save('x', makeSnapshot('x'));
    await store.save('y', makeSnapshot('y'));
    await store.delete('x');
    expect(await store.list()).toEqual(['y']);
  });
});

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

describe('Session', () => {
  let store: InMemorySessionStore;

  beforeEach(() => {
    store = new InMemorySessionStore();
  });

  // -------------------------------------------------------------------------
  // Construction
  // -------------------------------------------------------------------------

  it('creates with a random UUID when no id is supplied', () => {
    const s1 = new Session({ store });
    const s2 = new Session({ store });
    expect(s1.id).toBeTruthy();
    expect(s2.id).toBeTruthy();
    expect(s1.id).not.toBe(s2.id);
  });

  it('uses the provided id', () => {
    const session = new Session({ id: 'my-session', store });
    expect(session.id).toBe('my-session');
  });

  it('starts with an empty message history', () => {
    const session = new Session({ store });
    expect(session.getHistory()).toEqual([]);
  });

  it('starts with an empty AgentState', () => {
    const session = new Session({ store });
    expect(session.state.keys()).toHaveLength(0);
  });

  it('uses the provided AgentState', () => {
    const state = new AgentState({ topic: 'billing' });
    const session = new Session({ state, store });
    expect(session.state.get('topic')).toBe('billing');
  });

  // -------------------------------------------------------------------------
  // addMessage / getHistory
  // -------------------------------------------------------------------------

  it('addMessage appends a message', () => {
    const session = new Session({ store });
    session.addMessage('user', 'Hello');
    expect(session.getHistory()).toEqual([{ role: 'user', content: 'Hello' }]);
  });

  it('addMessage appends multiple messages in order', () => {
    const session = new Session({ store });
    session.addMessage('user', 'Hello');
    session.addMessage('assistant', 'Hi there');
    session.addMessage('user', 'How are you?');

    const history = session.getHistory();
    expect(history).toHaveLength(3);
    expect(history[0]).toEqual({ role: 'user', content: 'Hello' });
    expect(history[1]).toEqual({ role: 'assistant', content: 'Hi there' });
    expect(history[2]).toEqual({ role: 'user', content: 'How are you?' });
  });

  it('getHistory returns a copy (mutations do not affect session)', () => {
    const session = new Session({ store });
    session.addMessage('user', 'test');
    const history = session.getHistory();
    history.push({ role: 'user', content: 'injected' });
    expect(session.getHistory()).toHaveLength(1);
  });

  // -------------------------------------------------------------------------
  // save / load round-trip
  // -------------------------------------------------------------------------

  it('save persists to store; load restores messages', async () => {
    const session = new Session({ id: 'round-trip', store });
    session.addMessage('user', 'question');
    session.addMessage('assistant', 'answer');
    await session.save();

    const restored = await Session.load('round-trip', store);
    expect(restored).not.toBeNull();
    expect(restored!.getHistory()).toEqual([
      { role: 'user', content: 'question' },
      { role: 'assistant', content: 'answer' },
    ]);
  });

  it('save persists state; load restores state', async () => {
    const session = new Session({ id: 'state-rt', store });
    session.state.set('topic', 'billing');
    session.state.set('count', 7);
    await session.save();

    const restored = await Session.load('state-rt', store);
    expect(restored!.state.get('topic')).toBe('billing');
    expect(restored!.state.get('count')).toBe(7);
  });

  it('load returns null for unknown id', async () => {
    const result = await Session.load('does-not-exist', store);
    expect(result).toBeNull();
  });

  it('loaded session has the same id', async () => {
    const session = new Session({ id: 'known-id', store });
    await session.save();
    const restored = await Session.load('known-id', store);
    expect(restored!.id).toBe('known-id');
  });

  it('load uses the default store when no store is provided', async () => {
    const customStore = new InMemorySessionStore();
    setDefaultSessionStore(customStore);

    const session = new Session();
    await session.save();

    const restored = await Session.load(session.id);
    expect(restored).not.toBeNull();
  });

  it('save can be called multiple times and reflects latest state', async () => {
    const session = new Session({ id: 'multi-save', store });
    session.addMessage('user', 'first');
    await session.save();

    session.addMessage('assistant', 'second');
    await session.save();

    const restored = await Session.load('multi-save', store);
    expect(restored!.getHistory()).toHaveLength(2);
  });

  // -------------------------------------------------------------------------
  // delete
  // -------------------------------------------------------------------------

  it('delete removes the session from the store', async () => {
    const session = new Session({ id: 'to-delete', store });
    await session.save();
    await session.delete();

    const loaded = await Session.load('to-delete', store);
    expect(loaded).toBeNull();
  });

  // -------------------------------------------------------------------------
  // setDefaultSessionStore / getDefaultSessionStore
  // -------------------------------------------------------------------------

  it('getDefaultSessionStore returns the current default', () => {
    const current = getDefaultSessionStore();
    expect(current).toBeDefined();
  });

  it('setDefaultSessionStore changes the default used by new Sessions', async () => {
    const newStore = new InMemorySessionStore();
    setDefaultSessionStore(newStore);

    const session = new Session({ id: 'default-test' });
    session.addMessage('user', 'hi');
    await session.save();

    const ids = await newStore.list();
    expect(ids).toContain('default-test');
  });

  it('setDefaultSessionStore replaces the previous default', () => {
    const storeA = new InMemorySessionStore();
    const storeB = new InMemorySessionStore();

    setDefaultSessionStore(storeA);
    expect(getDefaultSessionStore()).toBe(storeA);

    setDefaultSessionStore(storeB);
    expect(getDefaultSessionStore()).toBe(storeB);
  });

  // -------------------------------------------------------------------------
  // Custom store via constructor
  // -------------------------------------------------------------------------

  it('session uses the provided store, not the default', async () => {
    const customStore = new InMemorySessionStore();
    const defaultStore = new InMemorySessionStore();
    setDefaultSessionStore(defaultStore);

    const session = new Session({ id: 'custom-store', store: customStore });
    await session.save();

    expect(await customStore.load('custom-store')).not.toBeNull();
    expect(await defaultStore.load('custom-store')).toBeNull();
  });

  // -------------------------------------------------------------------------
  // Custom SessionStore implementation
  // -------------------------------------------------------------------------

  it('Session.load works with a custom SessionStore implementation', async () => {
    const snapshots = new Map<string, SessionSnapshot>();
    const customStore: SessionStore = {
      async save(id, data) { snapshots.set(id, data); },
      async load(id) { return snapshots.get(id) ?? null; },
      async delete(id) { snapshots.delete(id); },
      async list() { return Array.from(snapshots.keys()); },
    };

    const session = new Session({ id: 'custom-impl', store: customStore });
    session.addMessage('user', 'test');
    await session.save();

    const restored = await Session.load('custom-impl', customStore);
    expect(restored!.getHistory()).toEqual([{ role: 'user', content: 'test' }]);
  });
});
