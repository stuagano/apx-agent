/**
 * Tests for hot-swap.ts — change a deployed agent's LLM via env-var update.
 *
 * Mirrors python/tests/test_hot_swap.py (control-side tests). Runtime-side
 * tests (resolveModel) are not ported — those live in the Python runtime
 * harness and have no direct TS equivalent.
 */

import { describe, it, expect, vi } from 'vitest';
import {
  APX_MODEL_OVERRIDE_ENV,
  getActiveOverride,
  hotSwapModel,
  type HotSwapResult,
} from '../src/hot-swap.js';
import type {
  EndpointInfo,
  ServingEndpointsClient,
  UpdateConfigPayload,
} from '../src/canary.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Build a fake served-entity response. environmentVars uses the camelCase
 * field name to match the SDK shape the production code expects.
 */
function _se(
  name: string,
  env: Record<string, string> = {},
): Record<string, unknown> {
  return {
    name,
    entityName: 'main.agents.my_agent',
    entityVersion: '3',
    workloadSize: 'Small',
    scaleToZeroEnabled: true,
    environmentVars: { ...env },
  };
}

function _makeClient(servedEntities: Record<string, unknown>[]): {
  client: ServingEndpointsClient;
  getMock: ReturnType<typeof vi.fn>;
  updateMock: ReturnType<typeof vi.fn>;
} {
  const info: EndpointInfo = {
    config: {
      // Cast: we feed extra fields (environmentVars) the response type
      // doesn't declare — production code reads them duck-typed.
      servedEntities: servedEntities as unknown as EndpointInfo['config'] extends infer C
        ? C extends { servedEntities?: infer S }
          ? S
          : never
        : never,
    },
  };
  const getMock = vi.fn(async () => info);
  const updateMock = vi.fn(async () => undefined);
  return {
    client: {
      get: getMock as unknown as ServingEndpointsClient['get'],
      updateConfig: updateMock as unknown as ServingEndpointsClient['updateConfig'],
    },
    getMock,
    updateMock,
  };
}

function _lastUpdatePayload(
  updateMock: ReturnType<typeof vi.fn>,
): UpdateConfigPayload {
  const call = updateMock.mock.calls[updateMock.mock.calls.length - 1];
  // [name, payload]
  return call[1] as UpdateConfigPayload;
}

// ---------------------------------------------------------------------------
// hotSwapModel
// ---------------------------------------------------------------------------

describe('hotSwapModel', () => {
  it('raises when no served entities', async () => {
    const { client } = _makeClient([]);
    await expect(
      hotSwapModel({
        endpoint: 'my-endpoint',
        model: 'databricks-claude-opus-4-7',
        servingEndpointsClient: client,
      }),
    ).rejects.toThrow(/no served entities/);
  });

  it('sets override when no prior value exists', async () => {
    const { client, updateMock } = _makeClient([_se('e1')]);

    const result: HotSwapResult = await hotSwapModel({
      endpoint: 'my-endpoint',
      model: 'databricks-claude-opus-4-7',
      servingEndpointsClient: client,
    });

    expect(result.endpoint).toBe('my-endpoint');
    expect(result.newModel).toBe('databricks-claude-opus-4-7');
    expect(result.previousModel).toBeNull();
    expect(result.servedEntities).toEqual(['e1']);

    expect(updateMock).toHaveBeenCalledOnce();
    const payload = _lastUpdatePayload(updateMock);
    expect(payload.servedEntities).toHaveLength(1);
    expect(payload.servedEntities?.[0].environmentVars).toEqual({
      [APX_MODEL_OVERRIDE_ENV]: 'databricks-claude-opus-4-7',
    });
  });

  it('captures previous override value from the first entity', async () => {
    const { client } = _makeClient([
      _se('e1', { [APX_MODEL_OVERRIDE_ENV]: 'databricks-claude-sonnet-4-6' }),
    ]);

    const result = await hotSwapModel({
      endpoint: 'my-endpoint',
      model: 'databricks-claude-opus-4-7',
      servingEndpointsClient: client,
    });
    expect(result.previousModel).toBe('databricks-claude-sonnet-4-6');
  });

  it('preserves other env vars per served entity', async () => {
    const { client, updateMock } = _makeClient([
      _se('e1', {
        OTHER_VAR: 'untouched',
        [APX_MODEL_OVERRIDE_ENV]: 'old-model',
        ANOTHER_VAR: 'also-untouched',
      }),
    ]);

    await hotSwapModel({
      endpoint: 'my-endpoint',
      model: 'new-model',
      servingEndpointsClient: client,
    });

    const payload = _lastUpdatePayload(updateMock);
    expect(payload.servedEntities?.[0].environmentVars).toEqual({
      OTHER_VAR: 'untouched',
      [APX_MODEL_OVERRIDE_ENV]: 'new-model',
      ANOTHER_VAR: 'also-untouched',
    });
  });

  it('updates multiple served entities (traffic split case)', async () => {
    const { client, updateMock } = _makeClient([
      _se('baseline'),
      _se('canary'),
    ]);

    const result = await hotSwapModel({
      endpoint: 'my-endpoint',
      model: 'new-model',
      servingEndpointsClient: client,
    });
    expect(result.servedEntities).toEqual(['baseline', 'canary']);

    const payload = _lastUpdatePayload(updateMock);
    expect(payload.servedEntities).toHaveLength(2);
    for (const e of payload.servedEntities ?? []) {
      expect(e.environmentVars?.[APX_MODEL_OVERRIDE_ENV]).toBe('new-model');
    }
  });

  it('honors a custom envVar name', async () => {
    const { client, updateMock } = _makeClient([_se('e1')]);

    await hotSwapModel({
      endpoint: 'my-endpoint',
      model: 'new-model',
      envVar: 'MY_CUSTOM_OVERRIDE',
      servingEndpointsClient: client,
    });

    const payload = _lastUpdatePayload(updateMock);
    expect(payload.servedEntities?.[0].environmentVars).toEqual({
      MY_CUSTOM_OVERRIDE: 'new-model',
    });
  });

  it('captures previous value from first entity even when later entities differ', async () => {
    const { client } = _makeClient([
      _se('e1', { [APX_MODEL_OVERRIDE_ENV]: 'first-old' }),
      _se('e2', { [APX_MODEL_OVERRIDE_ENV]: 'second-old' }),
    ]);

    const result = await hotSwapModel({
      endpoint: 'my-endpoint',
      model: 'new-model',
      servingEndpointsClient: client,
    });
    // Matches Python: previous value comes from the FIRST entity that has it.
    expect(result.previousModel).toBe('first-old');
  });

  it('calls updateConfig with the right shape', async () => {
    const { client, updateMock } = _makeClient([_se('e1')]);

    await hotSwapModel({
      endpoint: 'my-endpoint',
      model: 'new-model',
      servingEndpointsClient: client,
    });

    expect(updateMock).toHaveBeenCalledWith(
      'my-endpoint',
      expect.objectContaining({
        servedEntities: expect.arrayContaining([
          expect.objectContaining({
            name: 'e1',
            entityName: 'main.agents.my_agent',
            entityVersion: '3',
            environmentVars: expect.objectContaining({
              [APX_MODEL_OVERRIDE_ENV]: 'new-model',
            }),
          }),
        ]),
      }),
    );
  });

  it('rejects empty endpoint via zod', async () => {
    const { client } = _makeClient([_se('e1')]);
    await expect(
      hotSwapModel({
        endpoint: '',
        model: 'm',
        servingEndpointsClient: client,
      }),
    ).rejects.toThrow();
  });

  it('rejects empty model via zod', async () => {
    const { client } = _makeClient([_se('e1')]);
    await expect(
      hotSwapModel({
        endpoint: 'my-endpoint',
        model: '',
        servingEndpointsClient: client,
      }),
    ).rejects.toThrow();
  });

  it('result is frozen', async () => {
    const { client } = _makeClient([_se('e1')]);
    const result = await hotSwapModel({
      endpoint: 'my-endpoint',
      model: 'm',
      servingEndpointsClient: client,
    });
    expect(Object.isFrozen(result)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// getActiveOverride
// ---------------------------------------------------------------------------

describe('getActiveOverride', () => {
  it('returns null when there are no served entities', async () => {
    const { client } = _makeClient([]);
    const v = await getActiveOverride({
      endpoint: 'my-endpoint',
      servingEndpointsClient: client,
    });
    expect(v).toBeNull();
  });

  it('returns null when no entity has the override env var', async () => {
    const { client } = _makeClient([_se('e1', { OTHER_VAR: 'x' })]);
    const v = await getActiveOverride({
      endpoint: 'my-endpoint',
      servingEndpointsClient: client,
    });
    expect(v).toBeNull();
  });

  it('returns the current override value from the first entity that has it', async () => {
    const { client } = _makeClient([
      _se('e1', { [APX_MODEL_OVERRIDE_ENV]: 'databricks-claude-opus-4-7' }),
    ]);
    const v = await getActiveOverride({
      endpoint: 'my-endpoint',
      servingEndpointsClient: client,
    });
    expect(v).toBe('databricks-claude-opus-4-7');
  });

  it('honors a custom envVar name', async () => {
    const { client } = _makeClient([
      _se('e1', { MY_CUSTOM_OVERRIDE: 'model-x' }),
    ]);
    const v = await getActiveOverride({
      endpoint: 'my-endpoint',
      servingEndpointsClient: client,
      envVar: 'MY_CUSTOM_OVERRIDE',
    });
    expect(v).toBe('model-x');
  });

  it('returns null when endpoint has no config at all', async () => {
    const getMock = vi.fn(async () => ({ config: null, pendingConfig: null }));
    const updateMock = vi.fn(async () => undefined);
    const client: ServingEndpointsClient = {
      get: getMock as unknown as ServingEndpointsClient['get'],
      updateConfig: updateMock as unknown as ServingEndpointsClient['updateConfig'],
    };
    const v = await getActiveOverride({
      endpoint: 'my-endpoint',
      servingEndpointsClient: client,
    });
    expect(v).toBeNull();
  });

  it('rejects empty endpoint via zod', async () => {
    const { client } = _makeClient([_se('e1')]);
    await expect(
      getActiveOverride({
        endpoint: '',
        servingEndpointsClient: client,
      }),
    ).rejects.toThrow();
  });
});
