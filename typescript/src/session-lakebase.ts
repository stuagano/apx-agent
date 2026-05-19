/**
 * LakebaseSessionStore — Postgres-backed session persistence over Databricks Lakebase.
 *
 * Lakebase is Databricks' managed Postgres. Compared to `DeltaSessionStore`:
 *
 *   - Lower per-operation latency (Postgres point lookups vs. SQL warehouse
 *     round-trips).
 *   - Better for high-frequency turns (chat UIs, interactive agents).
 *   - Worse for cheap durability across long-idle sessions.
 *
 * The store takes injectable Postgres-style executors (query + exec). The
 * caller wires up the actual transport — `pg`, `postgres`, a Databricks
 * Postgres proxy, whatever — typically with a `do_connect`-style hook that
 * mints fresh OAuth tokens via `ws.postgres.generate_database_credential()`.
 * The store stays narrow: SQL only.
 *
 * Schema (created on first use when `autoCreate=true`):
 *
 * ```sql
 * CREATE TABLE IF NOT EXISTS {table_name} (
 *   session_id  TEXT PRIMARY KEY,
 *   history     TEXT NOT NULL,    -- JSON-encoded list of message dicts
 *   state       TEXT NOT NULL,    -- JSON-encoded dict
 *   created_at  DOUBLE PRECISION,
 *   updated_at  DOUBLE PRECISION
 * )
 * ```
 */

import { z } from 'zod';
import type { Message } from './workflows/types.js';
import type { SessionSnapshot, SessionStore } from './workflows/session.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Row-returning executor — runs a parameterized SELECT and returns rows. */
export type PgQueryExecutor = (
  sql: string,
  params: Record<string, unknown>,
) => Promise<Array<Record<string, unknown>>>;

/** Side-effecting executor — runs parameterized INSERT/UPDATE/DELETE/DDL. */
export type PgExecExecutor = (
  sql: string,
  params: Record<string, unknown>,
) => Promise<void>;

const optionsSchema = z.object({
  tableName: z.string().min(1).optional(),
  autoCreate: z.boolean().optional(),
});

export interface LakebaseSessionStoreOptions {
  /** Row-returning executor for parameterized SELECT. */
  querySql: PgQueryExecutor;
  /** Side-effecting executor for parameterized DDL / DML. */
  execSql: PgExecExecutor;
  /** Table name (may include schema prefix). Defaults to `"apx_sessions"`. */
  tableName?: string;
  /** Run `CREATE TABLE IF NOT EXISTS` on first use. Defaults to true. */
  autoCreate?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function isoToEpoch(iso: string): number {
  if (!iso) return 0;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms / 1000 : 0;
}

function epochToIso(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return new Date(0).toISOString();
  }
  return new Date(seconds * 1000).toISOString();
}

// ---------------------------------------------------------------------------
// LakebaseSessionStore
// ---------------------------------------------------------------------------

export class LakebaseSessionStore implements SessionStore {
  readonly tableName: string;
  private readonly querySql: PgQueryExecutor;
  private readonly execSql: PgExecExecutor;
  private autoCreate: boolean;
  private created = false;

  constructor(options: LakebaseSessionStoreOptions) {
    optionsSchema.parse({ tableName: options.tableName, autoCreate: options.autoCreate });
    this.tableName = options.tableName ?? 'apx_sessions';
    this.querySql = options.querySql;
    this.execSql = options.execSql;
    this.autoCreate = options.autoCreate ?? true;
  }

  // -- table creation -----------------------------------------------------

  private async ensureTable(): Promise<void> {
    if (this.created || !this.autoCreate) return;
    // Flip the flag before issuing the DDL so we don't loop on a busted
    // permission/path setup (mirrors Python).
    this.created = true;
    const ddl =
      `CREATE TABLE IF NOT EXISTS ${this.tableName} (` +
      `  session_id TEXT PRIMARY KEY,` +
      `  history TEXT NOT NULL,` +
      `  state TEXT NOT NULL,` +
      `  created_at DOUBLE PRECISION,` +
      `  updated_at DOUBLE PRECISION` +
      `)`;
    try {
      await this.execSql(ddl, {});
    } catch (e) {
      console.warn(
        `LakebaseSessionStore: CREATE TABLE IF NOT EXISTS ${this.tableName} failed: ${String(e)} — ` +
          `subsequent reads/writes may fail until the table exists.`,
      );
    }
  }

  // -- SessionStore protocol --------------------------------------------

  async load(id: string): Promise<SessionSnapshot | null> {
    await this.ensureTable();
    const sql =
      `SELECT session_id, history, state, created_at, updated_at ` +
      `FROM ${this.tableName} WHERE session_id = :sid LIMIT 1`;
    let rows: Array<Record<string, unknown>>;
    try {
      rows = await this.querySql(sql, { sid: id });
    } catch (e) {
      console.warn(`LakebaseSessionStore.load(${id}) failed: ${String(e)} — returning null.`);
      return null;
    }
    if (!rows || rows.length === 0) return null;
    const row = rows[0]!;
    let messages: Message[];
    let state: Record<string, unknown>;
    try {
      messages = JSON.parse((row.history as string) || '[]') as Message[];
      state = JSON.parse((row.state as string) || '{}') as Record<string, unknown>;
    } catch (e) {
      console.warn(
        `LakebaseSessionStore.load(${id}): malformed JSON: ${String(e)} — returning empty session.`,
      );
      messages = [];
      state = {};
    }
    return {
      id: String(row.session_id ?? id),
      messages,
      state,
      createdAt: epochToIso(Number(row.created_at ?? 0)),
      updatedAt: epochToIso(Number(row.updated_at ?? 0)),
    };
  }

  async save(id: string, data: SessionSnapshot): Promise<void> {
    await this.ensureTable();
    const sql =
      `INSERT INTO ${this.tableName} ` +
      `(session_id, history, state, created_at, updated_at) ` +
      `VALUES (:sid, :history, :state, :created_at, :updated_at) ` +
      `ON CONFLICT (session_id) DO UPDATE SET ` +
      `  history = EXCLUDED.history, ` +
      `  state = EXCLUDED.state, ` +
      `  updated_at = EXCLUDED.updated_at`;
    await this.execSql(sql, {
      sid: id,
      history: JSON.stringify(data.messages ?? []),
      state: JSON.stringify(data.state ?? {}),
      created_at: isoToEpoch(data.createdAt),
      updated_at: isoToEpoch(data.updatedAt),
    });
  }

  async delete(id: string): Promise<void> {
    await this.ensureTable();
    const sql = `DELETE FROM ${this.tableName} WHERE session_id = :sid`;
    try {
      await this.execSql(sql, { sid: id });
    } catch (e) {
      console.warn(`LakebaseSessionStore.delete(${id}) failed: ${String(e)}`);
    }
  }

  async list(): Promise<string[]> {
    await this.ensureTable();
    const sql = `SELECT session_id FROM ${this.tableName}`;
    try {
      const rows = await this.querySql(sql, {});
      return rows.map((r) => String(r.session_id));
    } catch (e) {
      console.warn(`LakebaseSessionStore.list() failed: ${String(e)} — returning empty list.`);
      return [];
    }
  }

  // -- internal test hooks ------------------------------------------------

  /** @internal — Test helper to disable auto-create (mirrors Python). */
  _disableAutoCreate(): void {
    this.autoCreate = false;
    this.created = true;
  }
}
