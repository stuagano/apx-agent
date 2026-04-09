/**
 * Agent plugin for Databricks AppKit.
 *
 * Registers typed tool functions, runs the agent loop via OpenAI Agents SDK
 * against Databricks Model Serving, and exposes /responses + /health endpoints.
 *
 * Usage:
 *   import { createApp, server } from '@databricks/appkit';
 *   import { agent } from 'appkit-agent';
 *
 *   createApp({
 *     plugins: [
 *       server(),
 *       agent({
 *         model: 'databricks-claude-sonnet-4-6',
 *         instructions: 'You are a helpful assistant.',
 *         tools: [getTableLineage, findJobsForTable],
 *       }),
 *     ],
 *   });
 */

import type { Express, Request, Response } from 'express';

import type { AgentTool, FunctionSchema } from './tools.js';
import { toolsToFunctionSchemas } from './tools.js';
import { runViaSDK, streamViaSDK, initDatabricksClient } from './runner.js';
import { createMcpToolProvider } from './mcp-client.js';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface AgentConfig {
  /** Model serving endpoint name (e.g. 'databricks-claude-sonnet-4-6'). */
  model: string;
  /** System prompt for the agent. */
  instructions?: string;
  /** Registered tool functions. */
  tools?: AgentTool[];
  /** Max tool-calling loop iterations. */
  maxIterations?: number;
  /** URLs of remote sub-agents (Databricks Apps). */
  subAgents?: string[];
  /**
   * Remote MCP server URLs to consume as tools.
   *
   * Tools are discovered at startup via tools/list and merged into the
   * agent's tool list alongside any statically registered tools.
   *
   * Supports Databricks managed MCP URLs:
   *   /api/2.0/mcp/genie/{space_id}              — Genie Space
   *   /api/2.0/mcp/functions/{catalog}/{schema}   — UC Functions
   *
   * @example
   * mcpServers: [
   *   'https://my-workspace.databricks.com/api/2.0/mcp/genie/abc123',
   *   'https://my-workspace.databricks.com/api/2.0/mcp/functions/main/default',
   * ]
   */
  mcpServers?: string[];
  /** API route prefix. Defaults to '/api/agent'. */
  apiPrefix?: string;
}

// ---------------------------------------------------------------------------
// Plugin exports (consumed by discovery, mcp, dev plugins)
// ---------------------------------------------------------------------------

export interface AgentExports {
  getTools: () => AgentTool[];
  getConfig: () => AgentConfig;
  getToolSchemas: () => FunctionSchema[];
}

// ---------------------------------------------------------------------------
// Request/Response types (Responses API format)
// ---------------------------------------------------------------------------

interface ResponsesInput {
  input: string | Array<{ role?: string; content?: string | Array<{ type?: string; text?: string }> }>;
  stream?: boolean;
  custom_inputs?: Record<string, unknown>;
}

interface ResponsesOutput {
  id: string;
  object: string;
  status: string;
  output: Array<{
    type: string;
    role: string;
    content: Array<{ type: string; text: string }>;
  }>;
  output_text: string;
}

// ---------------------------------------------------------------------------
// Helper: extract OBO headers from request
// ---------------------------------------------------------------------------

function getOboHeaders(req: Request): Record<string, string> {
  return {
    authorization: (req.headers.authorization as string) ?? '',
    'x-forwarded-access-token': (req.headers['x-forwarded-access-token'] as string) ?? '',
    'x-forwarded-host': (req.headers['x-forwarded-host'] as string) ?? '',
    'x-forwarded-user': (req.headers['x-forwarded-user'] as string) ?? '',
  };
}

// ---------------------------------------------------------------------------
// Helper: parse Responses API input
// ---------------------------------------------------------------------------

function parseInput(raw: ResponsesInput): Array<{ role: string; content: string }> {
  const input = raw.input;
  if (typeof input === 'string') {
    return [{ role: 'user', content: input }];
  }
  return input.map((item) => {
    const role = item.role ?? 'user';
    let content = item.content ?? '';
    if (Array.isArray(content)) {
      content = content
        .filter((p) => p.type === 'input_text' || p.type === 'text')
        .map((p) => p.text ?? '')
        .join(' ');
    }
    return { role, content: String(content) };
  });
}

// ---------------------------------------------------------------------------
// Plugin factory
// ---------------------------------------------------------------------------

/**
 * Create the agent plugin.
 *
 * This returns a plain plugin object compatible with AppKit's createApp().
 * When AppKit's class-based Plugin API is confirmed and stable, this can
 * be converted to extend Plugin<AgentConfig>.
 */
export function createAgentPlugin(config: AgentConfig) {
  // Mutable — MCP tools are appended during setup()
  const tools: AgentTool[] = [...(config.tools ?? [])];
  const toolMap = new Map(tools.map((t) => [t.name, t]));
  const apiPrefix = config.apiPrefix ?? '/api/agent';

  let app: Express;

  return {
    name: 'agent' as const,
    displayName: 'Agent Plugin',
    description: 'AI agent with typed tools and deterministic routing',

    async setup(expressApp: Express) {
      app = expressApp;
      initDatabricksClient();

      // Discover tools from remote MCP servers and merge into the tool list
      if (config.mcpServers && config.mcpServers.length > 0) {
        try {
          const mcpTools = await createMcpToolProvider(config.mcpServers);
          for (const mcpTool of mcpTools) {
            if (!toolMap.has(mcpTool.name)) {
              tools.push(mcpTool);
              toolMap.set(mcpTool.name, mcpTool);
            } else {
              console.warn(
                `[agent] MCP tool "${mcpTool.name}" conflicts with an existing tool — skipping`,
              );
            }
          }
        } catch (err) {
          // Non-fatal: agent still starts, just without the MCP tools
          console.warn('[agent] MCP tool discovery failed:', err instanceof Error ? err.message : String(err));
        }
      }
    },

    injectRoutes(router: { get: Function; post: Function; all: Function }) {
      // Health check
      router.get(`${apiPrefix}/health`, (_req: Request, res: Response) => {
        res.json({ status: 'ok' });
      });

      // Individual tool endpoints (for loopback dispatch + direct use)
      for (const tool of tools) {
        router.post(`${apiPrefix}/tools/${tool.name}`, async (req: Request, res: Response) => {
          try {
            const result = await tool.handler(req.body);
            res.json(result);
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            res.status(500).json({ error: message });
          }
        });
      }

      // Primary endpoint — Responses API format
      router.post('/responses', async (req: Request, res: Response) => {
        try {
          const raw = req.body as ResponsesInput;
          const messages = parseInput(raw);
          const oboHeaders = getOboHeaders(req);

          if (raw.stream) {
            // SSE streaming
            res.setHeader('Content-Type', 'text/event-stream');
            res.setHeader('Cache-Control', 'no-cache');
            res.setHeader('Connection', 'keep-alive');

            const itemId = 'msg_001';
            res.write(`event: response.output_item.start\ndata: ${JSON.stringify({ item_id: itemId })}\n\n`);

            let fullText = '';
            try {
              for await (const chunk of streamViaSDK({
                model: config.model,
                instructions: config.instructions ?? '',
                messages,
                tools,
                subAgents: config.subAgents,
                maxTurns: config.maxIterations,
                app,
                oboHeaders,
                apiPrefix,
              })) {
                fullText += chunk;
                res.write(`event: output_text.delta\ndata: ${JSON.stringify({ item_id: itemId, text: chunk })}\n\n`);
              }

              const output = {
                type: 'message',
                role: 'assistant',
                content: [{ type: 'output_text', text: fullText }],
              };
              res.write(`event: response.output_item.done\ndata: ${JSON.stringify({ item_id: itemId, output })}\n\n`);
            } catch (err) {
              const message = err instanceof Error ? err.message : String(err);
              res.write(`event: error\ndata: ${JSON.stringify({ item_id: itemId, error: message })}\n\n`);
            }

            res.end();
            return;
          }

          // Non-streaming
          const text = await runViaSDK({
            model: config.model,
            instructions: config.instructions ?? '',
            messages,
            tools,
            subAgents: config.subAgents,
            maxTurns: config.maxIterations,
            app,
            oboHeaders,
            apiPrefix,
          });

          const response: ResponsesOutput = {
            id: `resp_${Date.now()}`,
            object: 'response',
            status: 'completed',
            output: [
              {
                type: 'message',
                role: 'assistant',
                content: [{ type: 'output_text', text }],
              },
            ],
            output_text: text,
          };

          res.json(response);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          res.status(500).json({ error: message });
        }
      });
    },

    exports(): AgentExports {
      return {
        getTools: () => tools,
        getConfig: () => config,
        getToolSchemas: () => toolsToFunctionSchemas(tools),
      };
    },
  };
}
