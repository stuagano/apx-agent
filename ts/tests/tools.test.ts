/**
 * Tests for tool definition and schema helpers.
 */

import { describe, it, expect, vi } from 'vitest';
import { z } from 'zod';
import {
  defineTool,
  zodToJsonSchema,
  toStrictSchema,
  toolsToFunctionSchemas,
} from '../src/agent/tools.js';

// ---------------------------------------------------------------------------
// defineTool
// ---------------------------------------------------------------------------

describe('defineTool', () => {
  it('preserves name, description, and parameters', () => {
    const schema = z.object({ tableName: z.string() });
    const tool = defineTool({
      name: 'get_lineage',
      description: 'Get table lineage',
      parameters: schema,
      handler: async () => 'ok',
    });

    expect(tool.name).toBe('get_lineage');
    expect(tool.description).toBe('Get table lineage');
    expect(tool.parameters).toBe(schema);
  });

  it('calls handler with parsed args', async () => {
    const handler = vi.fn(async ({ count }: { count: number }) => count * 2);
    const tool = defineTool({
      name: 'doubler',
      description: 'Doubles a number',
      parameters: z.object({ count: z.number() }),
      handler,
    });

    const result = await tool.handler({ count: 5 });
    expect(result).toBe(10);
    expect(handler).toHaveBeenCalledWith({ count: 5 });
  });

  it('validates input through the Zod schema before calling handler', async () => {
    const tool = defineTool({
      name: 'strict_tool',
      description: 'Strict schema tool',
      parameters: z.object({ value: z.string().min(1) }),
      handler: async ({ value }) => value.toUpperCase(),
    });

    // Valid call succeeds
    await expect(tool.handler({ value: 'hello' })).resolves.toBe('HELLO');

    // Invalid call fails Zod parse
    await expect(tool.handler({ value: '' })).rejects.toThrow();
  });

  it('returns the handler result as-is', async () => {
    const payload = { rows: [1, 2, 3] };
    const tool = defineTool({
      name: 'fetch_data',
      description: 'Fetch data',
      parameters: z.object({}),
      handler: async () => payload,
    });

    const result = await tool.handler({});
    expect(result).toEqual(payload);
  });

  it('supports async handlers that throw', async () => {
    const tool = defineTool({
      name: 'failing_tool',
      description: 'Always fails',
      parameters: z.object({ input: z.string() }),
      handler: async () => {
        throw new Error('upstream failure');
      },
    });

    await expect(tool.handler({ input: 'x' })).rejects.toThrow('upstream failure');
  });
});

// ---------------------------------------------------------------------------
// zodToJsonSchema
// ---------------------------------------------------------------------------

describe('zodToJsonSchema', () => {
  it('converts a simple object schema', () => {
    const schema = z.object({ name: z.string(), age: z.number() });
    const json = zodToJsonSchema(schema);

    expect(json.type).toBe('object');
    expect(json.properties).toBeDefined();
    const props = json.properties as Record<string, unknown>;
    expect((props.name as Record<string, unknown>).type).toBe('string');
    expect((props.age as Record<string, unknown>).type).toBe('number');
  });

  it('converts a string schema', () => {
    const json = zodToJsonSchema(z.string());
    expect(json.type).toBe('string');
  });

  it('converts an enum schema', () => {
    const json = zodToJsonSchema(z.enum(['a', 'b', 'c']));
    expect(json.enum).toEqual(['a', 'b', 'c']);
  });

  it('converts an optional field', () => {
    const schema = z.object({ tag: z.string().optional() });
    const json = zodToJsonSchema(schema);
    expect(json.type).toBe('object');
  });

  it('converts nested objects', () => {
    const schema = z.object({
      outer: z.object({ inner: z.boolean() }),
    });
    const json = zodToJsonSchema(schema);
    const props = json.properties as Record<string, Record<string, unknown>>;
    expect(props.outer.type).toBe('object');
  });
});

// ---------------------------------------------------------------------------
// toStrictSchema
// ---------------------------------------------------------------------------

describe('toStrictSchema', () => {
  it('returns empty strict schema for null input', () => {
    const result = toStrictSchema(null);
    expect(result).toEqual({
      type: 'object',
      properties: {},
      required: [],
      additionalProperties: false,
    });
  });

  it('returns empty strict schema for undefined input', () => {
    const result = toStrictSchema(undefined);
    expect(result).toEqual({
      type: 'object',
      properties: {},
      required: [],
      additionalProperties: false,
    });
  });

  it('adds additionalProperties: false to object schemas', () => {
    const schema: Record<string, unknown> = {
      type: 'object',
      properties: { name: { type: 'string' } },
      required: ['name'],
    };
    const result = toStrictSchema(schema);
    expect(result.additionalProperties).toBe(false);
  });

  it('does not mutate the original schema', () => {
    const schema: Record<string, unknown> = {
      type: 'object',
      properties: { x: { type: 'string' } },
    };
    const original = { ...schema };
    toStrictSchema(schema);
    expect(schema).toEqual(original);
  });

  it('recursively applies to nested object properties', () => {
    const schema: Record<string, unknown> = {
      type: 'object',
      properties: {
        nested: {
          type: 'object',
          properties: { inner: { type: 'string' } },
        },
      },
    };
    const result = toStrictSchema(schema);
    const props = result.properties as Record<string, Record<string, unknown>>;
    expect(props.nested.additionalProperties).toBe(false);
  });

  it('does not touch non-object nested properties', () => {
    const schema: Record<string, unknown> = {
      type: 'object',
      properties: {
        count: { type: 'number' },
        label: { type: 'string' },
      },
    };
    const result = toStrictSchema(schema);
    const props = result.properties as Record<string, Record<string, unknown>>;
    expect(props.count).toEqual({ type: 'number' });
    expect(props.label).toEqual({ type: 'string' });
  });

  it('adds required from property keys when required is absent', () => {
    const schema: Record<string, unknown> = {
      type: 'object',
      properties: {
        a: { type: 'string' },
        b: { type: 'number' },
      },
    };
    const result = toStrictSchema(schema);
    const required = result.required as string[];
    expect(required).toContain('a');
    expect(required).toContain('b');
  });

  it('preserves existing required array', () => {
    const schema: Record<string, unknown> = {
      type: 'object',
      properties: { a: { type: 'string' }, b: { type: 'number' } },
      required: ['a'],
    };
    const result = toStrictSchema(schema);
    expect(result.required).toEqual(['a']);
  });

  it('passes through non-object schemas unchanged (except spreading)', () => {
    const schema: Record<string, unknown> = { type: 'string' };
    const result = toStrictSchema(schema);
    expect(result.type).toBe('string');
    expect(result.additionalProperties).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// toolsToFunctionSchemas
// ---------------------------------------------------------------------------

describe('toolsToFunctionSchemas', () => {
  it('returns OpenAI function calling format for each tool', () => {
    const tools = [
      defineTool({
        name: 'search',
        description: 'Search the catalog',
        parameters: z.object({ query: z.string() }),
        handler: async () => [],
      }),
    ];

    const schemas = toolsToFunctionSchemas(tools);
    expect(schemas).toHaveLength(1);
    expect(schemas[0].type).toBe('function');
    expect(schemas[0].function.name).toBe('search');
    expect(schemas[0].function.description).toBe('Search the catalog');
  });

  it('applies strict schema (additionalProperties: false) to parameters', () => {
    const tools = [
      defineTool({
        name: 'lookup',
        description: 'Lookup a record',
        parameters: z.object({ id: z.string() }),
        handler: async () => null,
      }),
    ];

    const schemas = toolsToFunctionSchemas(tools);
    const params = schemas[0].function.parameters;
    expect(params.additionalProperties).toBe(false);
  });

  it('converts multiple tools correctly', () => {
    const tools = [
      defineTool({
        name: 'tool_a',
        description: 'Tool A',
        parameters: z.object({ x: z.string() }),
        handler: async () => 'a',
      }),
      defineTool({
        name: 'tool_b',
        description: 'Tool B',
        parameters: z.object({ y: z.number() }),
        handler: async () => 'b',
      }),
    ];

    const schemas = toolsToFunctionSchemas(tools);
    expect(schemas).toHaveLength(2);
    expect(schemas.map((s) => s.function.name)).toEqual(['tool_a', 'tool_b']);
  });

  it('returns empty array for empty tools list', () => {
    expect(toolsToFunctionSchemas([])).toEqual([]);
  });

  it('produces parameters with type object for object schemas', () => {
    const tools = [
      defineTool({
        name: 'act',
        description: 'Act on input',
        parameters: z.object({ action: z.string(), count: z.number() }),
        handler: async () => 'done',
      }),
    ];

    const schemas = toolsToFunctionSchemas(tools);
    expect(schemas[0].function.parameters.type).toBe('object');
  });
});
