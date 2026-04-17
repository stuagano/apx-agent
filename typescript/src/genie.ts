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
  attachments?: Array<{
    text?: { content?: string };
  }>;
}

// ---------------------------------------------------------------------------
// Genie polling helper
// ---------------------------------------------------------------------------

const TERMINAL_STATUSES = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
const POLL_INTERVAL_MS = 2000;
const MAX_POLLS = 30;

async function queryGenie(
  host: string,
  token: string,
  spaceId: string,
  question: string,
): Promise<string> {
  const conv = await dbFetch<GenieStartConversationResponse>(
    `${host}/api/2.0/genie/spaces/${spaceId}/start_conversation`,
    { token, method: 'POST', body: { content: question } },
  );

  const { conversation_id: convId, message_id: msgId } = conv;

  let msgResp: GenieMessageResponse = { status: '' };
  for (let i = 0; i < MAX_POLLS; i++) {
    msgResp = await dbFetch<GenieMessageResponse>(
      `${host}/api/2.0/genie/spaces/${spaceId}/conversations/${convId}/messages/${msgId}`,
      { token, method: 'GET' },
    );

    if (TERMINAL_STATUSES.has(msgResp.status)) break;
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }

  if (msgResp.status === 'FAILED' || msgResp.status === 'CANCELLED') {
    return `Genie query ${msgResp.status.toLowerCase()}.`;
  }

  for (const att of msgResp.attachments ?? []) {
    if (att.text?.content) return att.text.content;
  }
  return '';
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
      const token = resolveToken(opts.oboHeaders);
      return queryGenie(host, token, spaceId, question);
    },
  });
}
