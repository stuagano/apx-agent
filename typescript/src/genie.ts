/**
 * genieTool — wrap a Genie space as a registered apx-agent tool.
 *
 * @example
 * import { genieTool } from 'appkit-agent';
 *
 * const agent = createAgentPlugin({
 *   model: 'databricks-claude-sonnet-4-6',
 *   tools: [genieTool('abc123', { description: 'Answer sales data questions' })],
 * });
 */

import { z } from 'zod';
import { defineTool } from './agent/tools.js';
import type { AgentTool } from './agent/tools.js';
import { resolveHost, resolveToken, dbFetch } from './connectors/types.js';

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

interface GenieStartConversationResponse {
  conversation_id: string;
  message_id: string;
}

interface GenieMessageResponse {
  status: string;
  error?: { message?: string };
  attachments?: Array<{
    attachment_id?: string;
    text?: { content?: string };
    query?: { query?: string };
  }>;
}

interface GenieQueryResultResponse {
  statement_response?: {
    manifest?: { schema?: { columns?: Array<{ name?: string }> } };
    result?: { data_array?: unknown[][] };
  };
}

// ---------------------------------------------------------------------------
// Genie polling helper
// ---------------------------------------------------------------------------

const TERMINAL_STATUSES = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 30;

interface GenieRunResult {
  message: GenieMessageResponse;
  convId: string;
  msgId: string;
}

async function runGenieQuery(
  host: string,
  token: string,
  spaceId: string,
  question: string,
): Promise<GenieRunResult> {
  const conv = await dbFetch<GenieStartConversationResponse>(
    `${host}/api/2.0/genie/spaces/${spaceId}/start_conversation`,
    { token, method: 'POST', body: { content: question } },
  );

  const { conversation_id: convId, message_id: msgId } = conv;

  let message: GenieMessageResponse = { status: '' };
  for (let i = 0; i < MAX_POLLS; i++) {
    message = await dbFetch<GenieMessageResponse>(
      `${host}/api/2.0/genie/spaces/${spaceId}/conversations/${convId}/messages/${msgId}`,
      { token, method: 'GET' },
    );

    if (TERMINAL_STATUSES.has(message.status)) break;
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }

  return { message, convId, msgId };
}

function extractText(msg: GenieMessageResponse): string {
  for (const att of msg.attachments ?? []) {
    if (att.text?.content) return att.text.content;
  }
  return '';
}

function extractGeneratedSql(msg: GenieMessageResponse): string {
  for (const att of msg.attachments ?? []) {
    if (att.query?.query) return att.query.query;
  }
  return '';
}

async function extractRows(
  host: string,
  token: string,
  spaceId: string,
  convId: string,
  msgId: string,
  msg: GenieMessageResponse,
): Promise<Array<Record<string, unknown>>> {
  const rows: Array<Record<string, unknown>> = [];
  for (const att of msg.attachments ?? []) {
    if (!att.query || !att.attachment_id) continue;
    try {
      const qr = await dbFetch<GenieQueryResultResponse>(
        `${host}/api/2.0/genie/spaces/${spaceId}/conversations/${convId}/messages/${msgId}/attachments/${att.attachment_id}/query-result`,
        { token, method: 'GET' },
      );
      const stmt = qr.statement_response;
      if (!stmt?.manifest?.schema?.columns || !stmt.result?.data_array) continue;
      const cols = stmt.manifest.schema.columns.map((c) => c.name ?? '');
      for (const row of stmt.result.data_array) {
        const obj: Record<string, unknown> = {};
        cols.forEach((c, i) => {
          obj[c] = row[i];
        });
        rows.push(obj);
      }
    } catch {
      // best-effort — skip failed attachment fetches
    }
  }
  return rows;
}

async function askGenie(
  host: string,
  token: string,
  spaceId: string,
  question: string,
): Promise<string> {
  const { message } = await runGenieQuery(host, token, spaceId, question);
  if (message.status === 'FAILED' || message.status === 'CANCELLED') {
    return `Genie query ${message.status.toLowerCase()}.`;
  }
  return extractText(message);
}

// ---------------------------------------------------------------------------
// Public factory
// ---------------------------------------------------------------------------

export interface GenieToolOptions {
  /** Tool name shown to the LLM. Defaults to `"ask_genie"`. */
  name?: string;
  /** Tool description shown to the LLM. */
  description?: string;
  /** Databricks workspace host. Falls back to `DATABRICKS_HOST` env var. */
  host?: string;
  /**
   * OBO headers forwarded from the incoming request.
   * Pass `req.headers` (as a plain object) to inherit the user's token.
   * Falls back to `DATABRICKS_TOKEN` env var for local dev.
   */
  oboHeaders?: Record<string, string>;
}

/**
 * Create an AgentTool that queries a Genie space by natural-language conversation.
 *
 * @param spaceId - Genie space ID (the UUID from the Genie space URL).
 * @param opts    - Optional overrides for name, description, host, and auth headers.
 */
export function genieTool(spaceId: string, opts: GenieToolOptions = {}): AgentTool {
  const name = opts.name ?? 'ask_genie';
  const description =
    opts.description ??
    `Ask a natural-language question to the Genie space and receive an answer. ` +
      `Use this for data questions that Genie can answer via SQL. (spaceId=${spaceId})`;

  return defineTool({
    name,
    description,
    parameters: z.object({ question: z.string().describe('The question to ask Genie') }),
    handler: async ({ question }) => {
      const host = resolveHost(opts.host);
      const token = await resolveToken(opts.oboHeaders);
      return askGenie(host, token, spaceId, question);
    },
  });
}


// ---------------------------------------------------------------------------
// genieQueryTool — returns structured results (rows + SQL + answer)
// ---------------------------------------------------------------------------

export interface GenieQueryToolOptions extends GenieToolOptions {
  /** Truncate sql_results to this many rows. Defaults to 20. */
  maxRows?: number;
}

export interface GenieQueryResult {
  question: string;
  sql_results: Array<Record<string, unknown>>;
  result_count: number;
  genie_response: string;
  generated_sql: string;
  error?: string;
  details?: string;
}

/**
 * Create an AgentTool that asks Genie a question and returns structured data.
 *
 * Unlike `genieTool`, the returned dict includes `sql_results` (the actual
 * rows), `generated_sql` (the SQL Genie produced), and `genie_response`
 * (the narrative answer). Use this when downstream agents need to reason
 * over the data — for example, a report-generator step that needs row-level
 * detail, not just the summary.
 */
export function genieQueryTool(spaceId: string, opts: GenieQueryToolOptions = {}): AgentTool {
  const name = opts.name ?? 'query_genie';
  const description =
    opts.description ??
    `Ask a natural-language question and receive both the answer and the ` +
      `underlying SQL result rows. Use for ad-hoc data exploration when you ` +
      `need the data itself, not just a summary. (spaceId=${spaceId})`;
  const maxRows = opts.maxRows ?? 20;

  return defineTool({
    name,
    description,
    parameters: z.object({ question: z.string().describe('The question to ask Genie') }),
    handler: async ({ question }): Promise<GenieQueryResult> => {
      const host = resolveHost(opts.host);
      const token = await resolveToken(opts.oboHeaders);

      try {
        const { message, convId, msgId } = await runGenieQuery(host, token, spaceId, question);
        if (message.status === 'FAILED' || message.status === 'CANCELLED') {
          return {
            question,
            sql_results: [],
            result_count: 0,
            genie_response: '',
            generated_sql: '',
            error: `Genie query ${message.status.toLowerCase()}`,
            details: message.error?.message,
          };
        }

        const rows = await extractRows(host, token, spaceId, convId, msgId, message);
        return {
          question,
          sql_results: rows.slice(0, maxRows),
          result_count: rows.length,
          genie_response: extractText(message).slice(0, 500),
          generated_sql: extractGeneratedSql(message),
        };
      } catch (err) {
        return {
          question,
          sql_results: [],
          result_count: 0,
          genie_response: '',
          generated_sql: '',
          error: `Genie query failed: ${(err as Error).message ?? String(err)}`,
        };
      }
    },
  });
}
