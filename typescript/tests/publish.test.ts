/**
 * Tests for publish.ts — Supervisor Agent publishing.
 *
 * Mirrors python/tests/test_publish.py.
 *
 * Covers:
 *   1. _slug normalises arbitrary strings into safe tool_id values.
 *   2. publishToSupervisor builds a Tool with toolType="serving_endpoint"
 *      and calls client.createTool with the correct parent + toolId.
 *   3. toolId defaults to a slug of the servingEndpoint name; explicit
 *      toolId wins when supplied.
 *   4. extraToolKwargs merges into the Tool (escape hatch for SDK evolution).
 *   5. createSupervisorAgent calls client.createSupervisorAgent with the
 *      displayName/description/instructions fields populated.
 *   6. Zod validation rejects missing required opts.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  _slug,
  createSupervisorAgent,
  publishToSupervisor,
  type SupervisorAgent,
  type SupervisorAgentsClient,
  type SupervisorTool,
} from '../src/publish.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeClient(): {
  client: SupervisorAgentsClient;
  createSupervisorAgentMock: ReturnType<typeof vi.fn>;
  createToolMock: ReturnType<typeof vi.fn>;
} {
  const createSupervisorAgentMock = vi.fn(
    async (opts: { supervisorAgent: SupervisorAgent }): Promise<SupervisorAgent> => ({
      ...opts.supervisorAgent,
      id: 'sa-created',
    }),
  );
  const createToolMock = vi.fn(
    async (opts: {
      parent: string;
      tool: SupervisorTool;
      toolId: string;
    }): Promise<SupervisorTool> => ({
      ...opts.tool,
      id: opts.toolId,
    }),
  );
  return {
    client: {
      createSupervisorAgent:
        createSupervisorAgentMock as unknown as SupervisorAgentsClient['createSupervisorAgent'],
      createTool: createToolMock as unknown as SupervisorAgentsClient['createTool'],
    },
    createSupervisorAgentMock,
    createToolMock,
  };
}

// ---------------------------------------------------------------------------
// _slug
// ---------------------------------------------------------------------------

describe('_slug', () => {
  it.each([
    ['data-triage', 'data_triage'],
    ['Data Triage', 'data_triage'],
    ['a.b.c', 'a_b_c'],
    ['___leading___', 'leading'],
    ['__', 'tool'], // fallback for empty result
    ['Foo-Bar 99', 'foo_bar_99'],
  ])('normalises %s -> %s', (raw, expected) => {
    expect(_slug(raw)).toBe(expected);
  });
});

// ---------------------------------------------------------------------------
// publishToSupervisor
// ---------------------------------------------------------------------------

describe('publishToSupervisor', () => {
  it('creates a tool with toolType=serving_endpoint, name, description', async () => {
    const { client, createToolMock } = makeClient();

    await publishToSupervisor({
      supervisorAgentId: 'sa-123',
      servingEndpoint: 'data-triage',
      description: 'Triages missing-data questions.',
      supervisorAgentsClient: client,
    });

    expect(createToolMock).toHaveBeenCalledOnce();
    const call = createToolMock.mock.calls[0][0];
    expect(call.parent).toBe('supervisor-agents/sa-123');
    expect(call.toolId).toBe('data_triage');
    expect(call.tool.toolType).toBe('serving_endpoint');
    expect(call.tool.name).toBe('data-triage');
    expect(call.tool.description).toBe('Triages missing-data questions.');
  });

  it('explicit toolId overrides slug', async () => {
    const { client, createToolMock } = makeClient();

    await publishToSupervisor({
      supervisorAgentId: 'sa-123',
      servingEndpoint: 'data-triage',
      description: '...',
      toolId: 'data_triage_v2',
      supervisorAgentsClient: client,
    });

    expect(createToolMock.mock.calls[0][0].toolId).toBe('data_triage_v2');
  });

  it('extraToolKwargs merges into the tool', async () => {
    const { client, createToolMock } = makeClient();

    await publishToSupervisor({
      supervisorAgentId: 'sa-123',
      servingEndpoint: 'data-triage',
      description: '...',
      supervisorAgentsClient: client,
      extraToolKwargs: { id: 'preview-only-field', future_field: true },
    });

    const tool = createToolMock.mock.calls[0][0].tool;
    expect(tool.extra).toBeDefined();
    expect(tool.extra?.id).toBe('preview-only-field');
    expect(tool.extra?.future_field).toBe(true);
    // Core fields still set
    expect(tool.toolType).toBe('serving_endpoint');
    expect(tool.name).toBe('data-triage');
  });

  it('uses servingEndpoint slug as toolId when not provided', async () => {
    const { client, createToolMock } = makeClient();

    await publishToSupervisor({
      supervisorAgentId: 'sa-1',
      servingEndpoint: 'Foo-Bar 99',
      description: '...',
      supervisorAgentsClient: client,
    });

    expect(createToolMock.mock.calls[0][0].toolId).toBe('foo_bar_99');
  });

  it('returns the response from the client', async () => {
    const { client } = makeClient();

    const result = await publishToSupervisor({
      supervisorAgentId: 'sa-1',
      servingEndpoint: 'agent-x',
      description: '...',
      supervisorAgentsClient: client,
    });

    expect(result.id).toBe('agent_x');
    expect(result.toolType).toBe('serving_endpoint');
  });

  it('rejects missing supervisorAgentId via Zod', async () => {
    const { client } = makeClient();
    await expect(
      // @ts-expect-error - intentionally missing required field
      publishToSupervisor({
        servingEndpoint: 'agent-x',
        description: '...',
        supervisorAgentsClient: client,
      }),
    ).rejects.toThrow();
  });

  it('rejects missing servingEndpoint via Zod', async () => {
    const { client } = makeClient();
    await expect(
      // @ts-expect-error - intentionally missing required field
      publishToSupervisor({
        supervisorAgentId: 'sa-1',
        description: '...',
        supervisorAgentsClient: client,
      }),
    ).rejects.toThrow();
  });

  it('rejects missing supervisorAgentsClient via Zod', async () => {
    await expect(
      // @ts-expect-error - intentionally missing required field
      publishToSupervisor({
        supervisorAgentId: 'sa-1',
        servingEndpoint: 'agent-x',
        description: '...',
      }),
    ).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// createSupervisorAgent
// ---------------------------------------------------------------------------

describe('createSupervisorAgent', () => {
  it('passes displayName/description/instructions to the client', async () => {
    const { client, createSupervisorAgentMock } = makeClient();

    await createSupervisorAgent({
      displayName: 'Data Ops Supervisor',
      description: 'Routes data-team queries.',
      instructions: 'Pick the right specialist.',
      supervisorAgentsClient: client,
    });

    expect(createSupervisorAgentMock).toHaveBeenCalledOnce();
    const arg = createSupervisorAgentMock.mock.calls[0][0].supervisorAgent;
    expect(arg.displayName).toBe('Data Ops Supervisor');
    expect(arg.description).toBe('Routes data-team queries.');
    expect(arg.instructions).toBe('Pick the right specialist.');
  });

  it('returns the response from the client', async () => {
    const { client } = makeClient();

    const result = await createSupervisorAgent({
      displayName: 'S1',
      description: 'd',
      instructions: 'i',
      supervisorAgentsClient: client,
    });

    expect(result.id).toBe('sa-created');
    expect(result.displayName).toBe('S1');
  });

  it('rejects missing displayName via Zod', async () => {
    const { client } = makeClient();
    await expect(
      // @ts-expect-error - intentionally missing required field
      createSupervisorAgent({
        description: 'd',
        instructions: 'i',
        supervisorAgentsClient: client,
      }),
    ).rejects.toThrow();
  });

  it('rejects missing supervisorAgentsClient via Zod', async () => {
    await expect(
      // @ts-expect-error - intentionally missing required field
      createSupervisorAgent({
        displayName: 'S1',
        description: 'd',
        instructions: 'i',
      }),
    ).rejects.toThrow();
  });
});
