/**
 * Agent plugin for Databricks AppKit.
 *
 * Registers typed tool functions, runs the agent loop via DatabricksOpenAI,
 * and exposes /invocations + /health endpoints.
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

import type { IAppRouter } from '@databricks/appkit';
import { z } from 'zod';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A tool function with metadata derived from its schema. */
export interface AgentTool {
  name: string;
  description: string;
  parameters: z.ZodType;
  handler: (args: unknown) => Promise<unknown>;
}

/** Configuration for the agent plugin. */
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
}

// ---------------------------------------------------------------------------
// Tool definition helper
// ---------------------------------------------------------------------------

/**
 * Define a typed agent tool.
 *
 * @example
 * const getLineage = defineTool({
 *   name: 'get_table_lineage',
 *   description: 'Get upstream sources for a table',
 *   parameters: z.object({ tableName: z.string() }),
 *   handler: async ({ tableName }) => {
 *     // query Unity Catalog lineage
 *   },
 * });
 */
export function defineTool<T extends z.ZodType>(opts: {
  name: string;
  description: string;
  parameters: T;
  handler: (args: z.infer<T>) => Promise<unknown>;
}): AgentTool {
  return {
    name: opts.name,
    description: opts.description,
    parameters: opts.parameters,
    handler: async (raw: unknown) => {
      const parsed = opts.parameters.parse(raw);
      return opts.handler(parsed);
    },
  };
}

// ---------------------------------------------------------------------------
// Tool schema conversion
// ---------------------------------------------------------------------------

/** Convert Zod schemas to OpenAI function tool format for model serving. */
function toolsToFunctionSchemas(tools: AgentTool[]) {
  return tools.map((t) => ({
    type: 'function' as const,
    function: {
      name: t.name,
      description: t.description,
      parameters: zodToJsonSchema(t.parameters),
    },
  }));
}

/** Minimal Zod → JSON Schema conversion for tool parameters. */
function zodToJsonSchema(schema: z.ZodType): Record<string, unknown> {
  // Zod v4 exposes .toJsonSchema() — use it if available, otherwise
  // fall back to a basic object schema.
  if ('toJsonSchema' in schema && typeof schema.toJsonSchema === 'function') {
    return schema.toJsonSchema() as Record<string, unknown>;
  }
  return { type: 'object', properties: {} };
}

// ---------------------------------------------------------------------------
// Agent plugin factory
// ---------------------------------------------------------------------------

/**
 * AppKit plugin that adds agent capabilities to a Databricks App.
 *
 * Mounts:
 * - POST /invocations — Responses API compatible endpoint
 * - GET /health — liveness check
 * - POST /api/tools/:name — individual tool endpoints
 */
export function agent(config: AgentConfig) {
  const tools = config.tools ?? [];
  const toolMap = new Map(tools.map((t) => [t.name, t]));
  const maxIterations = config.maxIterations ?? 10;

  return {
    name: 'agent',
    displayName: 'Agent Plugin',
    description: 'AI agent with typed tools and deterministic routing',

    async setup() {
      // Plugin initialization — validate config, warm connections
    },

    injectRoutes(router: IAppRouter) {
      // Health check
      router.get('/health', (_req, res) => {
        res.json({ status: 'ok' });
      });

      // Individual tool endpoints
      for (const tool of tools) {
        router.post(`/api/tools/${tool.name}`, async (req, res) => {
          try {
            const result = await tool.handler(req.body);
            res.json(result);
          } catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            res.status(500).json({ error: message });
          }
        });
      }

      // Responses API endpoint — the agent loop
      router.post('/invocations', async (req, res) => {
        try {
          const { input, stream } = req.body;

          // TODO: Replace with DatabricksOpenAI.responses.create() when
          // Supervisor SDK lands in AppKit. For now, use the raw endpoint
          // to maintain control over deterministic routing.
          const result = await runAgentLoop({
            model: config.model,
            instructions: config.instructions,
            messages: normalizeInput(input),
            tools,
            toolMap,
            maxIterations,
            // Pass OBO headers through
            authHeaders: {
              authorization: req.headers.authorization ?? '',
              'x-forwarded-access-token':
                req.headers['x-forwarded-access-token'] ?? '',
            },
          });

          res.json({
            output: [
              {
                type: 'message',
                role: 'assistant',
                content: [{ type: 'output_text', text: result }],
              },
            ],
          });
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          res.status(500).json({ error: message });
        }
      });
    },

    /** Expose tools list for other plugins (discovery, MCP). */
    exports() {
      return {
        getTools: () => tools,
        getConfig: () => config,
        getToolSchemas: () => toolsToFunctionSchemas(tools),
      };
    },
  };
}

// ---------------------------------------------------------------------------
// Agent loop (placeholder — will be replaced by DatabricksOpenAI)
// ---------------------------------------------------------------------------

interface LoopParams {
  model: string;
  instructions?: string;
  messages: Array<{ role: string; content: string }>;
  tools: AgentTool[];
  toolMap: Map<string, AgentTool>;
  maxIterations: number;
  authHeaders: Record<string, string>;
}

function normalizeInput(
  input: string | Array<{ role: string; content: string }>
): Array<{ role: string; content: string }> {
  if (typeof input === 'string') {
    return [{ role: 'user', content: input }];
  }
  return input;
}

async function runAgentLoop(params: LoopParams): Promise<string> {
  // TODO: This is a placeholder. The real implementation will use
  // DatabricksOpenAI.responses.create() with server-side tool execution,
  // or the Supervisor SDK when it ships in AppKit.
  //
  // For now, this returns a stub to validate the plugin wiring.
  const toolNames = params.tools.map((t) => t.name).join(', ');
  const lastMessage = params.messages[params.messages.length - 1];
  return (
    `[Agent stub] Model: ${params.model}, ` +
    `Tools: [${toolNames}], ` +
    `Input: "${lastMessage?.content ?? '(empty)'}"`
  );
}
