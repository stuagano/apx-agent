/**
 * example-mining — extract few-shot examples from real session history.
 *
 * Walks a `SessionStore`, pairs (user, assistant) message turns, and
 * writes them as `Example` rows into an `ExampleStore`. Useful for
 * cold-starting a few-shot bank from real conversations.
 *
 * Turn-pairing rules:
 *   - We iterate messages in order. For each `user` message, we look
 *     forward and emit a `Turn` for the **next** message whose role is
 *     `assistant` AND whose `content` is non-empty.
 *   - Messages with any other role between them (e.g. `tool`, `system`,
 *     assistant messages that only carry tool_calls and no content) are
 *     skipped — they're scaffolding, not the demonstration response.
 *   - After emitting an assistant turn, we resume scanning for the next
 *     `user` message. Multiple user→assistant pairs per session are fine.
 *   - We never pair across sessions.
 *
 * Same injectable-dependency philosophy as the rest of the framework:
 * the caller wires `intentFn` / `scoreFn` / `tagsFn` / `metadataFn` /
 * `filterFn` — typically backed by an LLM classifier or a simple regex —
 * via plain callables. No Anthropic / Databricks SDK import lives here.
 *
 * `dryRun` materializes Examples client-side (via `materializeExample`)
 * without writing to the store. Returned Examples carry **client-side
 * generated ids** (UUID-prefixed) — these ids will NOT match what the
 * store would mint on a real `add()`. Callers MUST treat dry-run ids
 * as opaque, single-use placeholders.
 */

import {
  materializeExample,
  type Example,
  type ExampleInput,
  type ExampleStore,
} from './example.js';
import type { SessionStore } from './workflows/session.js';
import type { Message } from './workflows/types.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * One mined (user, assistant) turn drawn from a session's history.
 *
 * `turnIndex` is the 0-based ordinal of this Turn within its session
 * (first user→assistant pair is index 0, second is 1, etc.) — NOT the
 * raw index into the message array. Useful for ranking/filtering "early
 * turn" vs "deep conversation" examples.
 */
export interface Turn {
  readonly sessionId: string;
  readonly userMessage: Message;
  readonly assistantMessage: Message;
  readonly turnIndex: number;
}

/** Result returned by `mineExamples`. Frozen. */
export interface MineResult {
  readonly sessionsScanned: number;
  readonly turnsConsidered: number;
  readonly examplesAdded: number;
  readonly examples: readonly Example[];
}

/** Options for `mineExamples`. */
export interface MineExamplesOptions {
  sessionStore: SessionStore;
  exampleStore: ExampleStore;
  agentId: string;

  /** When set, only mine these session ids; else use `sessionStore.list()`. */
  sessionIds?: readonly string[];

  /** Classify the intent for a turn. Default: returns `'default'`. */
  intentFn?: (turn: Turn) => Promise<string> | string;

  /** Optional quality scoring. Returning `undefined` means "unscored". */
  scoreFn?: (turn: Turn) => Promise<number | undefined> | number | undefined;

  /**
   * Per-turn include filter. Default: include only turns where the
   * assistant content is non-empty after trim.
   */
  filterFn?: (turn: Turn) => boolean;

  /** Optional tag extractor. */
  tagsFn?: (turn: Turn) => readonly string[];

  /** Optional metadata. The mined `sessionId` + `turnIndex` are merged in. */
  metadataFn?: (turn: Turn) => Record<string, unknown>;

  /** Max examples to add per run. */
  limit?: number;

  /**
   * Discard turns whose `scoreFn` returned a number strictly less than
   * this. Turns with `scoreFn` returning `undefined` pass the filter.
   * Ignored when `scoreFn` is not supplied.
   */
  minScore?: number;

  /**
   * When true, materialize Examples client-side and return them WITHOUT
   * writing to the store. Ids in the returned Examples are placeholders
   * minted client-side; they will NOT match what the store would mint.
   */
  dryRun?: boolean;
}

// ---------------------------------------------------------------------------
// Defaults
// ---------------------------------------------------------------------------

function defaultFilter(turn: Turn): boolean {
  return turn.assistantMessage.content.trim().length > 0;
}

function defaultIntent(_turn: Turn): string {
  return 'default';
}

// ---------------------------------------------------------------------------
// Pairing
// ---------------------------------------------------------------------------

/**
 * Walk a session's messages and emit (user, assistant) Turn pairs.
 * Exported so tests + callers can reproduce the pairing without going
 * through the full mining pipeline.
 */
export function pairTurns(sessionId: string, messages: readonly Message[]): Turn[] {
  const turns: Turn[] = [];
  let turnIndex = 0;
  let pendingUser: Message | null = null;
  for (const m of messages) {
    if (m.role === 'user') {
      pendingUser = m;
      continue;
    }
    if (m.role === 'assistant' && pendingUser !== null) {
      if (m.content && m.content.length > 0) {
        turns.push({
          sessionId,
          userMessage: pendingUser,
          assistantMessage: m,
          turnIndex,
        });
        turnIndex += 1;
        pendingUser = null;
      }
      // assistant with empty content (tool-call only) — keep waiting for
      // a real assistant reply.
    }
    // All other roles (tool, system, etc.) — skip.
  }
  return turns;
}

// ---------------------------------------------------------------------------
// Mining
// ---------------------------------------------------------------------------

export async function mineExamples(opts: MineExamplesOptions): Promise<MineResult> {
  const {
    sessionStore,
    exampleStore,
    agentId,
    sessionIds,
    intentFn = defaultIntent,
    scoreFn,
    filterFn = defaultFilter,
    tagsFn,
    metadataFn,
    limit,
    minScore,
    dryRun = false,
  } = opts;

  const ids = sessionIds
    ? [...sessionIds]
    : await sessionStore.list();

  let sessionsScanned = 0;
  let turnsConsidered = 0;
  const out: Example[] = [];

  outer: for (const sid of ids) {
    const snap = await sessionStore.load(sid);
    if (!snap) continue;
    sessionsScanned += 1;
    const turns = pairTurns(sid, snap.messages);

    for (const turn of turns) {
      turnsConsidered += 1;
      if (!filterFn(turn)) continue;

      const intent = await intentFn(turn);
      const score = scoreFn ? await scoreFn(turn) : undefined;
      if (
        minScore !== undefined &&
        score !== undefined &&
        score < minScore
      ) {
        continue;
      }

      const tags = tagsFn ? tagsFn(turn) : undefined;
      const extraMeta = metadataFn ? metadataFn(turn) : {};
      const metadata = {
        ...extraMeta,
        sessionId: turn.sessionId,
        turnIndex: turn.turnIndex,
      };

      const input: ExampleInput = {
        agentId,
        input: turn.userMessage.content,
        output: turn.assistantMessage.content,
        intent,
        score,
        tags,
        metadata,
      };

      if (dryRun) {
        out.push(materializeExample(input));
      } else {
        const ex = await exampleStore.add(input);
        out.push(ex);
      }

      if (limit !== undefined && out.length >= limit) {
        break outer;
      }
    }
  }

  return Object.freeze({
    sessionsScanned,
    turnsConsidered,
    examplesAdded: dryRun ? 0 : out.length,
    examples: Object.freeze([...out]),
  });
}
