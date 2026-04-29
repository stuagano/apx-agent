/**
 * Agent plugin for Databricks AppKit.
 *
 * Registers typed tool functions, runs the agent loop via OpenAI Agents SDK
 * against Databricks Model Serving, and exposes /responses + /health endpoints.
 *
 * Usage:
 *   import { createApp, server } from '@databricks/appkit';
 *   import { createAgentPlugin } from 'appkit-agent';
 *
 *   createApp({
 *     plugins: [
 *       server(),
 *       createAgentPlugin({
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
import { runWithContext } from './request-context.js';
import { createTrace, addSpan, endTrace, truncate, traceHeadersIn, TRACE_ID_HEADER } from '../trace.js';
import { createMcpToolProvider } from './mcp-client.js';
import type { Runnable, Message } from '../workflows/types.js';
import { AgentState } from '../workflows/state.js';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

export interface AgentConfig {
  /** Human-readable agent name (used in traces). */
  name?: string;
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
   * A workflow agent (SequentialAgent, ParallelAgent, etc.) to use as the
   * execution engine for /responses. When set, the plugin delegates to
   * `workflow.run()` instead of the default LLM tool-calling loop.
   */
  workflow?: Runnable;
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
      // Health check — also at /api/health for bearer-token access
      const healthHandler = (_req: Request, res: Response) => {
        res.json({ status: 'ok' });
      };
      router.get(`${apiPrefix}/health`, healthHandler);
      router.get('/api/health', healthHandler);

      // Individual tool endpoints (for loopback dispatch + direct use)
      for (const tool of tools) {
        router.post(`${apiPrefix}/tools/${tool.name}`, async (req: Request, res: Response) => {
          try {
            const oboHeaders = getOboHeaders(req);
            const result = await runWithContext({ oboHeaders }, () => tool.handler(req.body));
            res.json(result);
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            res.status(500).json({ error: message });
          }
        });
      }

      // Primary endpoint — Responses API format.
      // Mounted at both /responses (interactive/SSO) and /api/responses
      // (bearer-token auth for app-to-app calls via the Databricks Apps gateway).
      const responsesHandler = async (req: Request, res: Response) => {
        const trace = createTrace(config.name ?? 'agent');

        // Pick up cross-agent trace context from inbound headers so the
        // dev UI can render parent → child trace links.
        const inbound = traceHeadersIn(req.headers);
        if (inbound.parentTraceId) trace.parentTraceId = inbound.parentTraceId;
        if (inbound.parentAgentName) trace.parentAgentName = inbound.parentAgentName;

        // Echo this trace's id back so the caller can link from its
        // agent_call span to this trace. Set before any res.write/json
        // so SSE and JSON responses both carry it.
        res.setHeader(TRACE_ID_HEADER, trace.id);

        addSpan(trace, { type: 'request', name: 'POST /responses', input: truncate(req.body) });

        try {
          const raw = req.body as ResponsesInput;
          const messages = parseInput(raw);
          const oboHeaders = getOboHeaders(req);

          // When a workflow agent is configured, delegate to it
          if (config.workflow) {
            const workflowMessages: Message[] = messages.map((m) => ({
              role: m.role,
              content: m.content,
            }));

            if (raw.stream && config.workflow.stream) {
              // SSE streaming via workflow
              res.setHeader('Content-Type', 'text/event-stream');
              res.setHeader('Cache-Control', 'no-cache');
              res.setHeader('Connection', 'keep-alive');

              const itemId = 'msg_001';
              res.write(`event: response.output_item.start\ndata: ${JSON.stringify({ item_id: itemId })}\n\n`);

              let fullText = '';
              try {
                await runWithContext({ oboHeaders, trace }, async () => {
                  for await (const chunk of config.workflow!.stream!(workflowMessages)) {
                    fullText += chunk;
                    res.write(`event: output_text.delta\ndata: ${JSON.stringify({ item_id: itemId, text: chunk })}\n\n`);
                  }
                });

                const output = {
                  type: 'message',
                  role: 'assistant',
                  content: [{ type: 'output_text', text: fullText }],
                };
                res.write(`event: response.output_item.done\ndata: ${JSON.stringify({ item_id: itemId, output })}\n\n`);
                addSpan(trace, { type: 'response', name: 'response', output: truncate(fullText) });
                endTrace(trace);
              } catch (err) {
                const errMsg = err instanceof Error ? err.message : String(err);
                res.write(`event: error\ndata: ${JSON.stringify({ item_id: itemId, error: errMsg })}\n\n`);
                endTrace(trace, 'error');
              }

              res.end();
              return;
            }

            // Non-streaming workflow
            const text = await runWithContext({ oboHeaders, trace }, () =>
              config.workflow!.run(workflowMessages),
            );

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

            addSpan(trace, { type: 'response', name: 'response', output: truncate(text) });
            endTrace(trace);
            res.json(response);
            return;
          }

          if (raw.stream) {
            // SSE streaming
            res.setHeader('Content-Type', 'text/event-stream');
            res.setHeader('Cache-Control', 'no-cache');
            res.setHeader('Connection', 'keep-alive');

            const itemId = 'msg_001';
            res.write(`event: response.output_item.start\ndata: ${JSON.stringify({ item_id: itemId })}\n\n`);

            let fullText = '';
            const heartbeat = setInterval(() => {
              res.write(': keepalive\n\n');
            }, 15_000);
            try {
              await runWithContext({ oboHeaders, trace }, async () => {
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
              });

              const output = {
                type: 'message',
                role: 'assistant',
                content: [{ type: 'output_text', text: fullText }],
              };
              res.write(`event: response.output_item.done\ndata: ${JSON.stringify({ item_id: itemId, output })}\n\n`);
              addSpan(trace, { type: 'response', name: 'response', output: truncate(fullText) });
              endTrace(trace);
            } catch (err) {
              const message = err instanceof Error ? err.message : String(err);
              res.write(`event: error\ndata: ${JSON.stringify({ item_id: itemId, error: message })}\n\n`);
              endTrace(trace, 'error');
            } finally {
              clearInterval(heartbeat);
            }

            res.end();
            return;
          }

          // Non-streaming
          const text = await runWithContext({ oboHeaders, trace }, () =>
            runViaSDK({
              model: config.model,
              instructions: config.instructions ?? '',
              messages,
              tools,
              subAgents: config.subAgents,
              maxTurns: config.maxIterations,
              app,
              oboHeaders,
              apiPrefix,
            }),
          );

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

          addSpan(trace, { type: 'response', name: 'response', output: truncate(text) });
          endTrace(trace);
          res.json(response);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          endTrace(trace, 'error');
          res.status(500).json({ error: message });
        }
      };
      router.post('/responses', responsesHandler);
      router.post('/api/responses', responsesHandler);
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
