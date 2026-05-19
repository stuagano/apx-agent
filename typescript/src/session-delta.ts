/**
 * DeltaSessionStore — UC Delta-backed session persistence.
 *
 * Persists session snapshots to a Unity Catalog Delta table. Suitable for
 * Model Serving (stateless replicas, durable storage required) and
 * multi-replica Apps deployments where in-memory state isn't shared.
 *
 * Table schema (created on first use when `autoCreate=true`):
 *
 * ```sql
 * CREATE TABLE IF NOT EXISTS {table_path} (
 *   session_id  STRING NOT NULL,
 *   history     STRING,    -- JSON-encoded list of message dicts
 *   state       STRING,    -- JSON-encoded dict
 *   created_at  DOUBLE,    -- Unix epoch seconds
 *   updated_at  DOUBLE
 * ) USING DELTA
 * ```
 *
 * The store takes two injectable executors: a `querySql` for SELECT (returns
 * rows) and an `execSql` for INSERT/MERGE/DELETE (no return). The caller
 * wires whatever Databricks SQL transport they prefer.
 *
 * Upserts use `MERGE INTO ... USING (SELECT ...) AS src ON
 * target.session_id = src.session_id WHEN MATCHED UPDATE ... WHEN NOT
 * MATCHED INSERT`. Idempotent under concurrent writes on different
 * session_ids; concurrent writes on the same session_id are last-write-wins.
 */

import { z } from 'zod';
import type { Message } from './workflows/types.js';
import type { SessionSnapshot, SessionStore } from './workflows/session.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Row-returning executor for SELECT statements. */
export type SqlQueryExecutor = (sql: string) => Promise<Array<Record<string, unknown>>>;

/** Side-effecting executor for CREATE/INSERT/MERGE/DELETE statements. */
export type SqlExecExecutor = (sql: string) => Promise<void>;

const optionsSchema = z.object({
  tablePath: z
    .string()
    .refine((s) => s.split('.').length === 3, {
      message: 'DeltaSessionStore tablePath must be a three-part UC name (catalog.schema.table).',
    }),
  autoCreate: z.boolean().optional(),
});

export interface DeltaSessionStoreOptions {
  /** Fully-qualified three-part UC table name, e.g. `"main.agents.sessions"`. */
  tablePath: string;
  /** SELECT executor — returns rows as plain objects keyed by column name. */
  querySql: SqlQueryExecutor;
  /** Side-effecting executor — runs CREATE/MERGE/DELETE statements. */
  execSql: SqlExecExecutor;
  /** Run `CREATE TABLE IF NOT EXISTS` on first use. Defaults to true. */
  autoCreate?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** SQL-escape a string literal — single quotes doubled. */
function sqlStr(value: string): string {
  return "'" + value.replace(/'/g, "''") + "'";
}

/** ISO string -> epoch seconds (as DOUBLE). Returns 0 for blank/invalid. */
function isoToEpoch(iso: string): number {
  if (!iso) return 0;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms / 1000 : 0;
}

/** Epoch seconds (DOUBLE) -> ISO string. */
function epochToIso(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) {
    return new Date(0).toISOString();
  }
  return new Date(seconds * 1000).toISOString();
}

// ---------------------------------------------------------------------------
// DeltaSessionStore
// ---------------------------------------------------------------------------

export class DeltaSessionStore implements SessionStore {
  readonly tablePath: string;
  private readonly querySql: SqlQueryExecutor;
  private readonly execSql: SqlExecExecutor;
  private readonly autoCreate: boolean;
  private created = false;

  constructor(options: DeltaSessionStoreOptions) {
    optionsSchema.parse({ tablePath: options.tablePath, autoCreate: options.autoCreate });
    this.tablePath = options.tablePath;
    this.querySql = options.querySql;
    this.execSql = options.execSql;
    this.autoCreate = options.autoCreate ?? true;
  }

  // -- table creation -----------------------------------------------------

  private async ensureTable(): Promise<void> {
    if (this.created || !this.autoCreate) return;
    // Flip the flag first so a failure here doesn't cause repeated retries
    // on every read/write (matches Python behaviour).
    this.created = true;
    const sql =
      `CREATE TABLE IF NOT EXISTS ${this.tablePath} (` +
      `  session_id STRING NOT NULL,` +
      `  history STRING,` +
      `  state STRING,` +
      `  created_at DOUBLE,` +
      `  updated_at DOUBLE` +
      `) USING DELTA`;
    try {
      await this.execSql(sql);
    } catch (e) {
      console.warn(
        `DeltaSessionStore: CREATE TABLE IF NOT EXISTS ${this.tablePath} failed: ${String(e)} — ` +
          `subsequent reads/writes may fail until the table exists.`,
      );
    }
  }

  // -- SessionStore protocol ---------------------------------------------

  async load(id: string): Promise<SessionSnapshot | null> {
    await this.ensureTable();
    const sql =
      `SELECT session_id, history, state, created_at, updated_at ` +
      `FROM ${this.tablePath} ` +
      `WHERE session_id = ${sqlStr(id)} LIMIT 1`;
    let rows: Array<Record<string, unknown>>;
    try {
      rows = await this.querySql(sql);
    } catch (e) {
      console.warn(`DeltaSessionStore.load(${id}) failed: ${String(e)} — returning null.`);
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
        `DeltaSessionStore.load(${id}): malformed JSON in row: ${String(e)} — returning empty session.`,
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
    const historyJson = JSON.stringify(data.messages ?? []);
    const stateJson = JSON.stringify(data.state ?? {});
    const createdAt = isoToEpoch(data.createdAt);
    const updatedAt = isoToEpoch(data.updatedAt);
    // MERGE keeps the upsert atomic. Source is a single-row inline SELECT
    // so we don't need a staging table.
    const sql =
      `MERGE INTO ${this.tablePath} AS target ` +
      `USING (SELECT ` +
      `  ${sqlStr(id)} AS session_id, ` +
      `  ${sqlStr(historyJson)} AS history, ` +
      `  ${sqlStr(stateJson)} AS state, ` +
      `  ${createdAt} AS created_at, ` +
      `  ${updatedAt} AS updated_at` +
      `) AS src ` +
      `ON target.session_id = src.session_id ` +
      `WHEN MATCHED THEN UPDATE SET ` +
      `  history = src.history, ` +
      `  state = src.state, ` +
      `  updated_at = src.updated_at ` +
      `WHEN NOT MATCHED THEN INSERT (` +
      `  session_id, history, state, created_at, updated_at` +
      `) VALUES (` +
      `  src.session_id, src.history, src.state, src.created_at, src.updated_at` +
      `)`;
    await this.execSql(sql);
  }

  async delete(id: string): Promise<void> {
    await this.ensureTable();
    const sql = `DELETE FROM ${this.tablePath} WHERE session_id = ${sqlStr(id)}`;
    try {
      await this.execSql(sql);
    } catch (e) {
      console.warn(`DeltaSessionStore.delete(${id}) failed: ${String(e)}`);
    }
  }

  async list(): Promise<string[]> {
    await this.ensureTable();
    const sql = `SELECT session_id FROM ${this.tablePath}`;
    try {
      const rows = await this.querySql(sql);
      return rows.map((r) => String(r.session_id));
    } catch (e) {
      console.warn(`DeltaSessionStore.list() failed: ${String(e)} — returning empty list.`);
      return [];
    }
  }
}
