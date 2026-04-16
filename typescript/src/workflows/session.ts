/**
 * Session — conversation session with message history, state, and pluggable persistence.
 *
 * A session ties together a conversation's messages and the shared AgentState
 * so workflow agents can accumulate context across turns.
 *
 * @example
 * const session = new Session();
 * session.addMessage('user', 'Analyze the billing table');
 * session.state.set('table', 'billing');
 *
 * // Save and restore
 * await session.save();
 * const restored = await Session.load(session.id);
 */

import { randomUUID } from 'node:crypto';
import { AgentState } from './state.js';
import type { Message } from './types.js';

// ---------------------------------------------------------------------------
// SessionStore — pluggable persistence interface
// ---------------------------------------------------------------------------

/**
 * Interface for session persistence backends.
 *
 * The default InMemorySessionStore is suitable for development and testing.
 * Implement this interface for durable storage (e.g., Lakebase, Redis, DynamoDB).
 */
export interface SessionStore {
  /** Persist a session snapshot. */
  save(id: string, data: SessionSnapshot): Promise<void>;

  /** Load a session snapshot by ID. Returns null if not found. */
  load(id: string): Promise<SessionSnapshot | null>;

  /** Delete a session by ID. */
  delete(id: string): Promise<void>;

  /** List all session IDs. */
  list(): Promise<string[]>;
}

/** Serializable snapshot of a session. */
export interface SessionSnapshot {
  id: string;
  messages: Message[];
  state: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

// ---------------------------------------------------------------------------
// InMemorySessionStore
// ---------------------------------------------------------------------------

/** Simple in-memory store for development. Data is lost on process restart. */
export class InMemorySessionStore implements SessionStore {
  private sessions = new Map<string, SessionSnapshot>();

  async save(id: string, data: SessionSnapshot): Promise<void> {
    this.sessions.set(id, structuredClone(data));
  }

  async load(id: string): Promise<SessionSnapshot | null> {
    const snap = this.sessions.get(id);
    return snap ? structuredClone(snap) : null;
  }

  async delete(id: string): Promise<void> {
    this.sessions.delete(id);
  }

  async list(): Promise<string[]> {
    return Array.from(this.sessions.keys());
  }
}

// ---------------------------------------------------------------------------
// Default store singleton
// ---------------------------------------------------------------------------

let defaultStore: SessionStore = new InMemorySessionStore();

/** Replace the default session store (e.g., with a Lakebase adapter). */
export function setDefaultSessionStore(store: SessionStore): void {
  defaultStore = store;
}

/** Get the current default session store. */
export function getDefaultSessionStore(): SessionStore {
  return defaultStore;
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

export class Session {
  readonly id: string;
  readonly state: AgentState;

  private messages: Message[];
  private store: SessionStore;
  private createdAt: string;
  private updatedAt: string;

  constructor(options?: {
    id?: string;
    state?: AgentState;
    store?: SessionStore;
  }) {
    this.id = options?.id ?? randomUUID();
    this.state = options?.state ?? new AgentState();
    this.store = options?.store ?? defaultStore;
    this.messages = [];
    this.createdAt = new Date().toISOString();
    this.updatedAt = this.createdAt;
  }

  /** Append a message to the conversation history. */
  addMessage(role: string, content: string): void {
    this.messages.push({ role, content });
    this.updatedAt = new Date().toISOString();
  }

  /** Return a copy of the conversation history. */
  getHistory(): Message[] {
    return [...this.messages];
  }

  /** Persist this session to the store. */
  async save(): Promise<void> {
    const snapshot: SessionSnapshot = {
      id: this.id,
      messages: [...this.messages],
      state: this.state.toObject(),
      createdAt: this.createdAt,
      updatedAt: new Date().toISOString(),
    };
    await this.store.save(this.id, snapshot);
  }

  /**
   * Load a session from the store.
   *
   * Returns a fully hydrated Session instance, or null if not found.
   */
  static async load(
    id: string,
    store?: SessionStore,
  ): Promise<Session | null> {
    const s = store ?? defaultStore;
    const snapshot = await s.load(id);
    if (!snapshot) return null;

    const state = new AgentState(snapshot.state);
    const session = new Session({ id: snapshot.id, state, store: s });

    // Restore messages and timestamps
    for (const msg of snapshot.messages) {
      session.messages.push(msg);
    }
    session.createdAt = snapshot.createdAt;
    session.updatedAt = snapshot.updatedAt;

    return session;
  }

  /** Delete this session from the store. */
  async delete(): Promise<void> {
    await this.store.delete(this.id);
  }
}
