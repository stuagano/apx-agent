/**
 * Tool definition and schema helpers.
 *
 * Define agent tools with Zod schemas. The schema is used for:
 * - OpenAI function calling (passed to Runner.run)
 * - MCP tool registration
 * - A2A discovery card skills
 * - Dev UI tool inspector
 */

import { z } from 'zod';
import { zodToJsonSchema as zodToJson } from 'zod-to-json-schema';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** A tool function with metadata derived from its Zod schema. */
export interface AgentTool {
  name: string;
  description: string;
  parameters: z.ZodType;
  handler: (args: unknown) => Promise<unknown>;
}

/** OpenAI function calling format. */
export interface FunctionSchema {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: Record<string, unknown>;
  };
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
// Schema conversion
// ---------------------------------------------------------------------------

/** Convert a Zod schema to JSON Schema, suitable for OpenAI function calling. */
export function zodToJsonSchema(schema: z.ZodType): Record<string, unknown> {
  // Zod v4 has native toJSONSchema() — use it if available
  if ('toJSONSchema' in schema && typeof (schema as any).toJSONSchema === 'function') {
    return (schema as any).toJSONSchema() as Record<string, unknown>;
  }
  // Fallback to zod-to-json-schema (works with Zod v3)
  try {
    return zodToJson(schema as any, { target: 'openAi' }) as Record<string, unknown>;
  } catch {
    return { type: 'object', properties: {} };
  }
}

/**
 * Ensure a JSON schema is "strict" for OpenAI — adds `additionalProperties: false`
 * on all object types recursively.
 */
export function toStrictSchema(schema: Record<string, unknown> | null | undefined): Record<string, unknown> {
  if (!schema) {
    return { type: 'object', properties: {}, required: [], additionalProperties: false };
  }
  const result = { ...schema };
  // Strip $schema — Databricks model serving rejects tool parameters with it
  delete result['$schema'];
  if (result.type === 'object') {
    result.additionalProperties = false;
    if (!result.required) {
      result.required = Object.keys((result.properties as Record<string, unknown>) ?? {});
    }
    if (result.properties && typeof result.properties === 'object') {
      result.properties = Object.fromEntries(
        Object.entries(result.properties as Record<string, unknown>).map(([k, v]) => {
          if (typeof v === 'object' && v !== null && (v as Record<string, unknown>).type === 'object') {
            return [k, toStrictSchema(v as Record<string, unknown>)];
          }
          return [k, v];
        }),
      );
    }
  }
  return result;
}

/** Convert AgentTools to OpenAI function calling format. */
export function toolsToFunctionSchemas(tools: AgentTool[]): FunctionSchema[] {
  return tools.map((t) => ({
    type: 'function' as const,
    function: {
      name: t.name,
      description: t.description,
      parameters: toStrictSchema(zodToJsonSchema(t.parameters)),
    },
  }));
}
