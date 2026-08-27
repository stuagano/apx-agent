import {
  createMockRequest,
  createTestPluginContext,
  mockServiceContext,
  setupDatabricksEnv,
} from '@databricks/appkit/testing';
import { describe, expect, it } from 'vitest';
import { z } from 'zod';

import {
  APX_APPKIT_PLUGIN_NAME,
  ApxAppKitGovernancePlugin,
  createApxAppKitAgentDefinition,
  type ApxAppKitAuditEvent,
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

describe('internal AppKit host', () => {
  it('does not expose AppKit host symbols from the public package interface', () => {
    expect(publicApi).not.toHaveProperty('APX_APPKIT_PLUGIN_NAME');
    expect(publicApi).not.toHaveProperty('ApxAppKitGovernancePlugin');
    expect(publicApi).not.toHaveProperty('apxAppKitGovernance');
    expect(publicApi).not.toHaveProperty('createApxAppKitAgentDefinition');
  });

  it('exposes APX tools as AppKit toolkit entries for agents()', () => {
    const apx = new ApxAppKitGovernancePlugin({
      agent: makeAgentExports(),
      toolAnnotations: {
        apply_recommendation: { effect: 'update', requiresUserContext: true },
      },
    });

    expect(apx.toolkit({ prefix: 'apx.' })).toMatchObject({
      'apx.lookup_policy': {
        __toolkitRef: true,
        pluginName: APX_APPKIT_PLUGIN_NAME,
        localName: 'lookup_policy',
        annotations: { effect: 'read', requiresUserContext: true },
        autoInheritable: true,
      },
      'apx.apply_recommendation': {
        __toolkitRef: true,
        pluginName: APX_APPKIT_PLUGIN_NAME,
        localName: 'apply_recommendation',
        annotations: { effect: 'update', requiresUserContext: true },
        autoInheritable: false,
      },
    });
  });

  it('creates an AppKit AgentDefinition that pulls tools from the APX plugin', () => {
    const agent = createApxAppKitAgentDefinition(makeAgentExports(), {
      default: true,
      toolPrefix: 'apx.',
    });
    const apx = new ApxAppKitGovernancePlugin({ agent: makeAgentExports() });

    if (typeof agent.tools !== 'function') throw new Error('expected function-form tools');
    const tools = agent.tools({ [APX_APPKIT_PLUGIN_NAME]: apx });

    expect(agent).toMatchObject({
      name: 'pricing-agent',
      instructions: 'Use APX governed tools.',
      model: 'databricks-claude-sonnet-4-5',
      default: true,
    });
    expect(tools).toHaveProperty('apx.lookup_policy');
  });

  it('enforces APX policy and records audit events around tool execution', async () => {
    const audit: ApxAppKitAuditEvent[] = [];
    const apx = new ApxAppKitGovernancePlugin({
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
    const apx = await mock.attach(new ApxAppKitGovernancePlugin({ agent: makeAgentExports() }));

    try {
      const result = await mock.ctx.executeTool(
        createMockRequest({ obo: { userId: 'alice@databricks.com' } }),
        APX_APPKIT_PLUGIN_NAME,
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
