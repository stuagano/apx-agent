import {
  createMockRequest,
  createTestPluginContext,
  mockServiceContext,
  setupDatabricksEnv,
} from '@databricks/appkit/testing';
import { describe, expect, it } from 'vitest';
import { z } from 'zod';

import {
  INTERNAL_APX_APPKIT_PLUGIN_NAME,
  InternalApxAppKitGovernancePlugin,
  createInternalApxAppKitAgentDefinition,
  createInternalApxAppKitAgentDefinitionFromManifest,
  type InternalApxAppKitAuditEvent,
  type InternalApxAppsHostManifest,
} from '../src/internal/appkit-host.js';
import {
  createAgentPlugin,
  defineTool,
} from '../src/index.js';
import * as publicApi from '../src/index.js';

function makeAgentExports() {
  const lookupPolicy = defineTool({
    name: 'lookup_policy',
    description: 'Return the policy attached to a governed APX resource.',
    parameters: z.object({ resource: z.string() }),
    handler: async ({ resource }) => ({ resource, policy: 'read-only' }),
  });
  const applyRecommendation = defineTool({
    name: 'apply_recommendation',
    description: 'Apply a governed pricing recommendation.',
    parameters: z.object({ recommendation_id: z.string() }),
    handler: async ({ recommendation_id }) => ({ recommendation_id, applied: true }),
  });
  return createAgentPlugin({
    name: 'pricing-agent',
    model: 'databricks-claude-sonnet-4-5',
    instructions: 'Use APX governed tools.',
    tools: [lookupPolicy, applyRecommendation],
  }).exports();
}

function makeManifest(): InternalApxAppsHostManifest {
  return {
    agent: {
      name: 'pricing-agent',
      model: 'databricks-claude-sonnet-4-5',
      instructions: 'Use APX governed tools.',
      max_iterations: 8,
    },
    appkit: {
      default: true,
      tool_prefix: 'apx.',
      max_steps: 8,
    },
    tools: [
      {
        name: 'lookup_policy',
        description: 'Return the policy attached to a governed APX resource.',
        parameters: {
          type: 'object',
          properties: { resource: { type: 'string' } },
          required: ['resource'],
          additionalProperties: false,
        },
        annotations: {
          effect: 'read',
          requires_user_context: true,
        },
      },
    ],
  };
}

describe('internal AppKit host', () => {
  it('does not expose AppKit host symbols from the public package interface', () => {
    expect(publicApi).not.toHaveProperty('APX_APPKIT_PLUGIN_NAME');
    expect(publicApi).not.toHaveProperty('ApxAppKitGovernancePlugin');
    expect(publicApi).not.toHaveProperty('apxAppKitGovernance');
    expect(publicApi).not.toHaveProperty('createApxAppKitAgentDefinition');
    expect(publicApi).not.toHaveProperty('INTERNAL_APX_APPKIT_PLUGIN_NAME');
    expect(publicApi).not.toHaveProperty('InternalApxAppKitGovernancePlugin');
    expect(publicApi).not.toHaveProperty('internalApxAppKitGovernance');
    expect(publicApi).not.toHaveProperty('createInternalApxAppKitAgentDefinition');
  });

  it('exposes APX tools as AppKit toolkit entries for agents()', () => {
    const apx = new InternalApxAppKitGovernancePlugin({
      agent: makeAgentExports(),
      toolAnnotations: {
        apply_recommendation: { effect: 'update', requiresUserContext: true },
      },
    });

    expect(apx.toolkit({ prefix: 'apx.' })).toMatchObject({
      'apx.lookup_policy': {
        __toolkitRef: true,
        pluginName: INTERNAL_APX_APPKIT_PLUGIN_NAME,
        localName: 'lookup_policy',
        annotations: { effect: 'read', requiresUserContext: true },
        autoInheritable: true,
      },
      'apx.apply_recommendation': {
        __toolkitRef: true,
        pluginName: INTERNAL_APX_APPKIT_PLUGIN_NAME,
        localName: 'apply_recommendation',
        annotations: { effect: 'update', requiresUserContext: true },
        autoInheritable: false,
      },
    });
  });

  it('creates an AppKit AgentDefinition that pulls tools from the APX plugin', () => {
    const agent = createInternalApxAppKitAgentDefinition(makeAgentExports(), {
      default: true,
      toolPrefix: 'apx.',
    });
    const apx = new InternalApxAppKitGovernancePlugin({ agent: makeAgentExports() });

    if (typeof agent.tools !== 'function') throw new Error('expected function-form tools');
    const tools = agent.tools({ [INTERNAL_APX_APPKIT_PLUGIN_NAME]: apx });

    expect(agent).toMatchObject({
      name: 'pricing-agent',
      instructions: 'Use APX governed tools.',
      model: 'databricks-claude-sonnet-4-5',
      default: true,
    });
    expect(tools).toHaveProperty('apx.lookup_policy');
  });

  it('creates an AppKit AgentDefinition from an APX host manifest', () => {
    const manifest = makeManifest();
    const agent = createInternalApxAppKitAgentDefinitionFromManifest(manifest);
    const apx = new InternalApxAppKitGovernancePlugin({ manifest });

    if (typeof agent.tools !== 'function') throw new Error('expected function-form tools');
    const tools = agent.tools({ [INTERNAL_APX_APPKIT_PLUGIN_NAME]: apx });

    expect(agent).toMatchObject({
      name: 'pricing-agent',
      instructions: 'Use APX governed tools.',
      model: 'databricks-claude-sonnet-4-5',
      default: true,
      maxSteps: 8,
    });
    expect(tools).toMatchObject({
      'apx.lookup_policy': {
        pluginName: INTERNAL_APX_APPKIT_PLUGIN_NAME,
        localName: 'lookup_policy',
        annotations: { effect: 'read', requiresUserContext: true },
        autoInheritable: true,
      },
    });
  });

  it('enforces APX policy and records audit events around tool execution', async () => {
    const audit: InternalApxAppKitAuditEvent[] = [];
    const apx = new InternalApxAppKitGovernancePlugin({
      agent: makeAgentExports(),
      policy: ({ toolName }) =>
        toolName === 'apply_recommendation'
          ? { action: 'DENY', reason: 'manual approval required' }
          : { action: 'ALLOW' },
      audit: (event) => audit.push(event),
    });

    await expect(apx.executeAgentTool('lookup_policy', { resource: 'main.sales.orders' })).resolves.toEqual({
      resource: 'main.sales.orders',
      policy: 'read-only',
    });
    await expect(
      apx.executeAgentTool('apply_recommendation', { recommendation_id: 'rec-1' }),
    ).rejects.toThrow('manual approval required');

    expect(audit).toMatchObject([
      { toolName: 'lookup_policy', action: 'ALLOW', reason: null },
      {
        toolName: 'apply_recommendation',
        action: 'DENY',
        reason: 'manual approval required',
      },
    ]);
  });

  it('runs APX plugin tools through AppKit PluginContext OBO dispatch', async () => {
    setupDatabricksEnv();
    const serviceContext = mockServiceContext({ userId: 'alice@databricks.com' });
    const mock = createTestPluginContext();
    const apx = await mock.attach(new InternalApxAppKitGovernancePlugin({ agent: makeAgentExports() }));

    try {
      const result = await mock.ctx.executeTool(
        createMockRequest({ obo: { userId: 'alice@databricks.com' } }),
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'lookup_policy',
        { resource: 'main.sales.orders' },
      );

      expect(result).toEqual({ resource: 'main.sales.orders', policy: 'read-only' });
      expect(serviceContext.createUserContextSpy).toHaveBeenCalled();
    } finally {
      serviceContext.restore();
    }
  });
});
