import { Plugin } from '@databricks/appkit';
import {
  createMockRequest,
  createMockResponse,
  createMockRouter,
  createTestPluginContext,
  expectStream,
  mockServiceContext,
  setupDatabricksEnv,
} from '@databricks/appkit/testing';
import {
  agents,
  createAgent,
  tool,
  type AgentAdapter,
} from '@databricks/appkit/beta';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';

import appsHostManifestFixture from './fixtures/apps-host-manifest.json';

import {
  INTERNAL_APX_APPKIT_PLUGIN_NAME,
  InternalApxAppKitGovernancePlugin,
  createInternalApxAppKitAgentDefinition,
  createInternalApxAppKitAgentDefinitionFromManifest,
  createInternalApxAppKitDevRuntime,
  internalApxAppKitSystemPrompt,
  internalApxAppKitAgentsOptionsFromManifest,
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
    kind: 'apx.apps_host_manifest',
    version: 1,
    agent: {
      name: 'pricing-agent',
      description: 'Governed pricing agent.',
      model: 'databricks-claude-sonnet-4-5',
      instructions: 'Use APX governed tools.',
      temperature: null,
      max_iterations: 8,
      max_tokens: null,
    },
    appkit: {
      default: true,
      tool_prefix: 'apx.',
      max_steps: 8,
      max_tokens: null,
      limits: {
        max_tool_calls: 8,
      },
      ephemeral: null,
      generation_params: null,
    },
    tools: [
      {
        name: 'lookup_policy',
        description: 'Return the policy attached to a governed APX resource.',
        runtime: 'python',
        parameters: {
          type: 'object',
          properties: { resource: { type: 'string' } },
          required: ['resource'],
          additionalProperties: false,
        },
        annotations: {
          effect: 'read',
          execution_identity: 'user',
          requires_request_context: true,
          requires_user_context: true,
        },
        output_schema: null,
        handler: { kind: 'python', ref: 'tools:lookup_policy' },
        resources: [{ kind: 'serving_endpoint', identifier: 'model-a' }],
        user_api_scopes: ['serving.serving-endpoints'],
      },
    ],
    resources: [
      { kind: 'job', identifier: 'telemetry-job' },
      { kind: 'serving_endpoint', identifier: 'model-a' },
    ],
    user_resources: [{ kind: 'serving_endpoint', identifier: 'model-a' }],
    service_resources: [{ kind: 'job', identifier: 'telemetry-job' }],
    user_api_scopes: ['serving.serving-endpoints'],
    app_to_app_permissions: [
      { url: 'https://peer.cloud.databricksapps.com', permission: 'CAN_USE' },
    ],
  };
}

function makeSupportedSurfaceManifest(): InternalApxAppsHostManifest {
  return {
    ...makeManifest(),
    tools: [
      {
        name: 'who_am_i',
        description: '',
        runtime: 'python',
        parameters: { type: 'object', properties: {}, additionalProperties: false },
        annotations: {
          effect: 'read',
          execution_identity: 'user',
          requires_request_context: true,
          requires_user_context: true,
        },
        output_schema: null,
        handler: { kind: 'python', ref: 'tools:who_am_i' },
        resources: [],
        user_api_scopes: [],
      },
      {
        name: 'apply_change',
        description: '',
        runtime: 'python',
        parameters: {
          type: 'object',
          properties: { value: { type: 'string' } },
          required: ['value'],
          additionalProperties: false,
        },
        annotations: {
          effect: 'update',
          execution_identity: 'service',
          requires_request_context: false,
          requires_user_context: false,
        },
        output_schema: null,
        handler: { kind: 'python', ref: 'tools:apply_change' },
        resources: [],
        user_api_scopes: [],
      },
      {
        name: 'remember',
        description: '',
        runtime: 'python',
        parameters: {
          type: 'object',
          properties: { value: { type: 'string' } },
          required: ['value'],
          additionalProperties: false,
        },
        annotations: {
          effect: 'update',
          execution_identity: 'service',
          requires_request_context: true,
          requires_user_context: false,
        },
        output_schema: null,
        handler: { kind: 'python', ref: 'tools:remember' },
        resources: [],
        user_api_scopes: [],
      },
    ],
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isResourceList(value: unknown): boolean {
  return Array.isArray(value) && value.every(
    (resource) => isRecord(resource)
      && typeof resource['kind'] === 'string'
      && typeof resource['identifier'] === 'string',
  );
}

function assertAppsHostManifest(
  value: unknown,
): asserts value is InternalApxAppsHostManifest {
  const agent = isRecord(value) ? value['agent'] : null;
  const appkit = isRecord(value) ? value['appkit'] : null;
  if (!isRecord(value)
    || value['kind'] !== 'apx.apps_host_manifest'
    || value['version'] !== 1
    || !isRecord(agent)
    || typeof agent['name'] !== 'string'
    || typeof agent['description'] !== 'string'
    || typeof agent['model'] !== 'string'
    || typeof agent['instructions'] !== 'string'
    || !(agent['temperature'] === null || typeof agent['temperature'] === 'number')
    || !(agent['max_tokens'] === null || typeof agent['max_tokens'] === 'number')
    || typeof agent['max_iterations'] !== 'number'
    || !isRecord(appkit)
    || typeof appkit['default'] !== 'boolean'
    || typeof appkit['tool_prefix'] !== 'string'
    || typeof appkit['max_steps'] !== 'number'
    || !(appkit['max_tokens'] === null || typeof appkit['max_tokens'] === 'number')
    || !isRecord(appkit['limits'])
    || typeof appkit['limits']['max_tool_calls'] !== 'number'
    || !(appkit['ephemeral'] === null || typeof appkit['ephemeral'] === 'boolean')
    || !(appkit['generation_params'] === null || isRecord(appkit['generation_params']))
    || !Array.isArray(value['tools'])
    || !isResourceList(value['resources'])
    || !isResourceList(value['user_resources'])
    || !isResourceList(value['service_resources'])
    || !Array.isArray(value['user_api_scopes'])
    || !value['user_api_scopes'].every((scope) => typeof scope === 'string')
    || !Array.isArray(value['app_to_app_permissions'])
    || !value['app_to_app_permissions'].every(
      (permission) => isRecord(permission)
        && typeof permission['url'] === 'string'
        && permission['permission'] === 'CAN_USE',
    )) {
    throw new Error('invalid APX Apps host manifest envelope');
  }
  for (const candidate of value['tools']) {
    if (!isRecord(candidate)
      || typeof candidate['name'] !== 'string'
      || typeof candidate['description'] !== 'string'
      || candidate['runtime'] !== 'python'
      || !isRecord(candidate['parameters'])
      || !(candidate['output_schema'] === null || isRecord(candidate['output_schema']))
      || !isRecord(candidate['annotations'])
      || typeof candidate['annotations']['effect'] !== 'string'
      || !['user', 'service'].includes(String(candidate['annotations']['execution_identity']))
      || typeof candidate['annotations']['requires_request_context'] !== 'boolean'
      || typeof candidate['annotations']['requires_user_context'] !== 'boolean'
      || !isRecord(candidate['handler'])
      || candidate['handler']['kind'] !== 'python'
      || typeof candidate['handler']['ref'] !== 'string'
      || !isResourceList(candidate['resources'])
      || !Array.isArray(candidate['user_api_scopes'])
      || !candidate['user_api_scopes'].every((scope) => typeof scope === 'string')) {
      throw new Error('invalid APX Apps host manifest tool');
    }
  }
}

async function waitForSseEvent(
  response: ReturnType<typeof createMockResponse>,
  eventType: string,
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + 2_000;
  while (Date.now() < deadline) {
    const body = response.write.mock.calls.map((call) => String(call[0])).join('');
    for (const line of body.split('\n')) {
      if (!line.startsWith('data: ')) continue;
      const event = JSON.parse(line.slice('data: '.length)) as Record<string, unknown>;
      if (event.type === eventType) return event;
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`Timed out waiting for SSE event: ${eventType}`);
}

describe('internal AppKit host', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

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
        annotations: { effect: 'update', requiresUserContext: true },
        autoInheritable: false,
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

  it('narrows Python generation parameters only at the AppKit adapter seam', () => {
    const manifest = makeManifest();
    manifest.appkit.generation_params = {
      temperature: 0.2,
      stop: ['DONE'],
    };

    expect(createInternalApxAppKitAgentDefinitionFromManifest(manifest))
      .toMatchObject({ generationParams: { temperature: 0.2, stop: ['DONE'] } });

    manifest.appkit.generation_params = { provider_extension: true };
    expect(() => createInternalApxAppKitAgentDefinitionFromManifest(manifest)).toThrow();
  });

  it('treats undeclared manifest tool effects as updates', () => {
    const manifest = makeManifest();
    Reflect.deleteProperty(manifest.tools[0].annotations, 'effect');
    const apx = new InternalApxAppKitGovernancePlugin({ manifest });

    expect(apx.toolkit({ prefix: 'apx.' })).toMatchObject({
      'apx.lookup_policy': {
        annotations: { effect: 'update', requiresUserContext: true },
        autoInheritable: false,
      },
    });
  });

  it('rebuilds the actual AppKit definition from live dev overrides', () => {
    const manifest = makeManifest();
    const dev = createInternalApxAppKitDevRuntime(manifest);

    expect(dev.snapshot()).toMatchObject({
      agentName: 'pricing-agent',
      model: 'databricks-claude-sonnet-4-5',
      originalModel: 'databricks-claude-sonnet-4-5',
      instructions: 'Use APX governed tools.',
      instructionsOverridden: false,
    });

    dev.setModel('databricks-claude-sonnet-4-6');
    dev.setInstructions('Prefer concise answers.');
    expect(dev.definition()).toMatchObject({
      model: 'databricks-claude-sonnet-4-6',
      instructions: 'Prefer concise answers.',
    });

    dev.setInstructions(null);
    expect(dev.definition()).toMatchObject({ instructions: 'Use APX governed tools.' });
    expect(dev.snapshot().instructionsOverridden).toBe(false);
  });

  it('enables only selected manifest tools in the rebuilt AppKit definition', () => {
    const manifest = makeManifest();
    const dev = createInternalApxAppKitDevRuntime(manifest);
    const apx = new InternalApxAppKitGovernancePlugin({ manifest });

    dev.setToolEnabled('lookup_policy', false);
    const agent = dev.definition();
    if (typeof agent.tools !== 'function') throw new Error('expected function-form tools');

    expect(agent.tools({ [INTERNAL_APX_APPKIT_PLUGIN_NAME]: apx })).not.toHaveProperty(
      'apx.lookup_policy',
    );
    expect(dev.snapshot().tools).toEqual([
      expect.objectContaining({ name: 'lookup_policy', enabled: false }),
    ]);
    expect(() => dev.setToolEnabled('missing', true)).toThrow('Unknown APX tool: missing');
  });

  it('adds bounded markdown skills as real read-only AppKit tools', async () => {
    const manifest = makeManifest();
    const dev = createInternalApxAppKitDevRuntime(manifest);
    const apx = new InternalApxAppKitGovernancePlugin({ manifest });

    dev.setSkill({
      name: 'pricing_policy',
      description: 'Load pricing policy guidance.',
      content: '# Pricing policy\nNever invent a discount.',
    });
    const agent = dev.definition();
    if (typeof agent.tools !== 'function') throw new Error('expected function-form tools');
    const tools = agent.tools({ [INTERNAL_APX_APPKIT_PLUGIN_NAME]: apx });
    const skill = tools['skill.pricing_policy'];

    expect(skill).toMatchObject({
      type: 'function',
      annotations: { effect: 'read', requiresUserContext: false },
    });
    if (!('execute' in skill)) throw new Error('expected executable skill tool');
    await expect(skill.execute({})).resolves.toBe(
      '# Pricing policy\nNever invent a discount.',
    );
    expect(() => dev.setSkill({ name: '../bad', description: 'bad', content: 'bad' })).toThrow();
    expect(() =>
      dev.setSkill({ name: 'too_large', description: 'large', content: 'x'.repeat(20_001) }),
    ).toThrow();
  });

  it('reports the exact system prompt configured on the rebuilt definition', () => {
    const manifest = makeManifest();
    const dev = createInternalApxAppKitDevRuntime(manifest);
    const definition = dev.definition();
    const context = {
      agentName: 'pricing-agent',
      pluginNames: [INTERNAL_APX_APPKIT_PLUGIN_NAME],
      toolNames: ['apx.lookup_policy'],
    };

    expect(definition.baseSystemPrompt).toBe(
      internalApxAppKitSystemPrompt('', context),
    );
    expect(dev.snapshot().systemPrompt).toBe(
      internalApxAppKitSystemPrompt('Use APX governed tools.', context),
    );
  });

  it('projects APX runtime limits into AppKit agents() options', () => {
    const options = internalApxAppKitAgentsOptionsFromManifest({
      ...makeManifest(),
      appkit: {
        ...makeManifest().appkit,
        limits: {
          max_tool_calls: 8,
          max_concurrent_streams_per_user: 2,
          max_sub_agent_depth: 1,
          tool_call_timeout_ms: 15_000,
        },
      },
    });

    expect(options).toEqual({
      approval: { requireForDestructive: true },
      limits: {
        maxToolCalls: 8,
        maxConcurrentStreamsPerUser: 2,
        maxSubAgentDepth: 1,
        toolCallTimeoutMs: 15_000,
      },
    });
  });

  it('does not add a second policy layer around APX tool execution', async () => {
    const apx = new InternalApxAppKitGovernancePlugin({ agent: makeAgentExports() });
    await expect(apx.executeAgentTool('lookup_policy', { resource: 'main.sales.orders' })).resolves.toEqual({
      resource: 'main.sales.orders',
      policy: 'read-only',
    });
  });

  it('executes manifest-backed Python tools through the configured bridge', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ result: { policy: 'read-only' } }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const apx = new InternalApxAppKitGovernancePlugin({
      manifest: makeManifest(),
      pythonBridge: {
        baseUrl: 'http://127.0.0.1:8000/',
        headers: { 'x-apx-test': '1' },
      },
    });

    await expect(apx.executeAgentTool('lookup_policy', { resource: 'main.sales.orders' })).resolves.toEqual({
      policy: 'read-only',
    });

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/_apx/internal/appkit/tools/lookup_policy',
      expect.objectContaining({
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-apx-test': '1',
        },
        body: JSON.stringify({ args: { resource: 'main.sales.orders' } }),
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps concrete service resources in DAB instead of AppKit plugin requirements', async () => {
    const manifest = makeManifest();
    manifest.service_resources.push({ kind: 'uc_table', identifier: 'main.sales.orders' });
    const mock = createTestPluginContext();

    await expect(mock.attach(new InternalApxAppKitGovernancePlugin({ manifest })))
      .resolves.toBeInstanceOf(InternalApxAppKitGovernancePlugin);
    expect(Reflect.has(InternalApxAppKitGovernancePlugin, 'getResourceRequirements')).toBe(false);
    expect(InternalApxAppKitGovernancePlugin.manifest.resources).toEqual({
      required: [],
      optional: [],
    });
    expect(manifest).toMatchObject({
      user_resources: [{ kind: 'serving_endpoint', identifier: 'model-a' }],
      service_resources: [
        { kind: 'job', identifier: 'telemetry-job' },
        { kind: 'uc_table', identifier: 'main.sales.orders' },
      ],
      user_api_scopes: ['serving.serving-endpoints'],
      app_to_app_permissions: [
        { url: 'https://peer.cloud.databricksapps.com', permission: 'CAN_USE' },
      ],
    });
  });

  it('accepts and round-trips the complete Python authorization manifest shape', () => {
    const rawManifest: unknown = appsHostManifestFixture;
    assertAppsHostManifest(rawManifest);
    const manifest = rawManifest;
    const roundTripped = JSON.parse(JSON.stringify(manifest));
    assertAppsHostManifest(roundTripped);
    const apx = new InternalApxAppKitGovernancePlugin({ manifest: roundTripped });

    expect(roundTripped).toEqual(appsHostManifestFixture);
    expect(roundTripped.tools[0]).toMatchObject({
      runtime: 'python',
      output_schema: { type: 'string' },
      handler: { kind: 'python', ref: 'tests.test_apps_host_manifest:search_orders' },
      resources: [{ kind: 'uc_table', identifier: 'main.sales.orders' }],
      user_api_scopes: ['sql'],
    });
    expect(apx.getAgentTools()[0]).toMatchObject({
      name: 'search_orders',
      annotations: { effect: 'update', requiresUserContext: true },
    });
    expect(InternalApxAppKitGovernancePlugin.manifest.resources).toEqual({
      required: [],
      optional: [],
    });
  });

  it('dispatches manifest tools by identity without duplicating the bridge path', async () => {
    setupDatabricksEnv();
    const serviceContext = mockServiceContext({ userId: 'alice@databricks.com' });
    const mock = createTestPluginContext();
    const manifest = makeManifest();
    manifest.tools = [
      ...(manifest.tools ?? []),
      {
        name: 'service_health',
        description: '',
        runtime: 'python',
        parameters: { type: 'object', properties: {}, additionalProperties: false },
        annotations: {
          effect: 'read',
          execution_identity: 'service',
          requires_request_context: false,
          requires_user_context: false,
        },
        output_schema: null,
        handler: { kind: 'python', ref: 'tools:service_health' },
        resources: [],
        user_api_scopes: [],
      },
      {
        name: 'service_audit',
        description: '',
        runtime: 'python',
        parameters: { type: 'object', properties: {}, additionalProperties: false },
        annotations: {
          effect: 'update',
          execution_identity: 'service',
          requires_request_context: true,
          requires_user_context: false,
        },
        output_schema: null,
        handler: { kind: 'python', ref: 'tools:service_audit' },
        resources: [],
        user_api_scopes: [],
      },
    ];
    await mock.attach(new InternalApxAppKitGovernancePlugin({
      manifest,
      pythonBridge: {
        baseUrl: 'http://127.0.0.1:8000',
        headers: {
          'x-apx-test': '1',
          'X-Forwarded-Access-Token': 'configured-service-token',
          'X-Forwarded-User': 'configured-user@databricks.com',
          'X-Request-ID': 'configured-request-id',
        },
      },
    }));
    const appKitAsUser = vi.spyOn(Plugin.prototype, 'asUser');
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => (
      new Response(JSON.stringify({ result: String(input).split('/').at(-1) }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    ));
    const serviceRequest = createMockRequest({
      obo: {
        userId: 'alice@databricks.com',
        token: 'request-service-token',
        email: 'alice@databricks.com',
      },
      headers: {
        'x-request-id': 'request-123',
        'x-not-forwarded': 'secret-metadata',
      },
    });
    const tokenlessRequest = createMockRequest({
      headers: { 'x-forwarded-user': 'alice@databricks.com' },
    });
    const serviceHeaderSpy = vi.spyOn(serviceRequest, 'header');

    try {
      await expect(mock.ctx.executeTool(
        serviceRequest,
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'service_health',
        {},
      )).resolves.toBe('service_health');
      expect(appKitAsUser).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenLastCalledWith(
        expect.stringMatching(/service_health$/),
        expect.objectContaining({
          headers: {
            'content-type': 'application/json',
            'x-apx-test': '1',
            'x-forwarded-user': 'configured-user@databricks.com',
            'x-request-id': 'configured-request-id',
          },
        }),
      );

      await expect(mock.ctx.executeTool(
        serviceRequest,
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'service_audit',
        {},
      )).resolves.toBe('service_audit');
      expect(appKitAsUser).not.toHaveBeenCalled();
      const serviceHeaders = fetchMock.mock.calls.at(-1)?.[1]?.headers as Record<string, string>;
      expect(serviceHeaders['x-request-id']).toBe('request-123');
      expect(serviceHeaders['x-forwarded-user']).toBe('alice@databricks.com');
      expect(serviceHeaders['x-forwarded-email']).toBe('alice@databricks.com');
      expect(Object.keys(serviceHeaders).filter(
        (name) => name.toLowerCase() === 'x-forwarded-user',
      )).toEqual(['x-forwarded-user']);
      expect(Object.keys(serviceHeaders).map((name) => name.toLowerCase()))
        .not.toContain('x-forwarded-access-token');
      expect(serviceHeaders).not.toHaveProperty('x-not-forwarded');
      expect(serviceHeaderSpy).not.toHaveBeenCalledWith('x-forwarded-access-token');

      await expect(mock.ctx.executeTool(
        tokenlessRequest,
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'lookup_policy',
        { resource: 'main.sales.orders' },
      )).rejects.toThrow(/user token/i);
      expect(appKitAsUser).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(2);

      await expect(mock.ctx.executeTool(
        createMockRequest({ obo: { userId: 'alice@databricks.com', token: 'alice-token' } }),
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'lookup_policy',
        { resource: 'main.sales.orders' },
      )).resolves.toBe('lookup_policy');
      expect(appKitAsUser).toHaveBeenCalledTimes(2);
      const userHeaders = fetchMock.mock.calls.at(-1)?.[1]?.headers as Record<string, string>;
      expect(userHeaders['x-forwarded-access-token']).toBe('alice-token');
      expect(Object.keys(userHeaders).filter(
        (name) => name.toLowerCase() === 'x-forwarded-access-token',
      )).toEqual(['x-forwarded-access-token']);
    } finally {
      serviceContext.restore();
    }
  });

  it('proves the supported manifest surface through the real AppKit context', async () => {
    setupDatabricksEnv();
    const serviceContext = mockServiceContext({ userId: 'alice@databricks.com' });
    const mock = createTestPluginContext();
    const manifest = makeSupportedSurfaceManifest();
    await mock.attach(new InternalApxAppKitGovernancePlugin({
      manifest,
      pythonBridge: { baseUrl: 'http://127.0.0.1:8000' },
    }));
    const definition = createInternalApxAppKitAgentDefinitionFromManifest(manifest);
    const apx = new InternalApxAppKitGovernancePlugin({ manifest });
    if (typeof definition.tools !== 'function') throw new Error('expected function-form tools');
    expect(definition.tools({ [INTERNAL_APX_APPKIT_PLUGIN_NAME]: apx })).toMatchObject({
      'apx.who_am_i': { annotations: { effect: 'read' } },
      'apx.apply_change': { annotations: { effect: 'update' } },
      'apx.remember': { annotations: { effect: 'update' } },
    });

    let forwardedSignal: AbortSignal | null = null;
    let cancellationObserved = false;
    let cancelledExecutionCompleted = false;
    let releaseInFlightFetch: (() => void) | undefined;
    const inFlightFetchStarted = new Promise<void>((resolve) => {
      releaseInFlightFetch = resolve;
    });
    let identityCalls = 0;
    const executed: string[] = [];
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      const args = JSON.parse(String(init?.body)).args;
      forwardedSignal = init?.signal as AbortSignal;
      if (url.endsWith('/who_am_i')) {
        identityCalls += 1;
        if (identityCalls === 2) {
          releaseInFlightFetch?.();
          return await new Promise<Response>((_resolve, reject) => {
            forwardedSignal?.addEventListener('abort', () => {
              cancellationObserved = true;
              reject(new DOMException('AppKit tool dispatch aborted', 'AbortError'));
            }, { once: true });
          });
        }
      }
      if (url.endsWith('/apply_change') && args.value === 'deny') {
        return new Response(JSON.stringify({ detail: 'Tool execution is denied' }), {
          status: 403,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (url.endsWith('/remember')) {
        return new Response(JSON.stringify({
          detail: 'APX AppKit bridge cannot execute stateful tool: remember',
        }), {
          status: 400,
          headers: { 'content-type': 'application/json' },
        });
      }
      executed.push(url.split('/').at(-1) ?? 'missing');
      return new Response(JSON.stringify({ result: 'alice@databricks.com' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    });
    const request = createMockRequest({
      obo: {
        userId: 'alice@databricks.com',
        token: 'alice-token',
        email: 'alice@databricks.com',
      },
    });

    try {
      await expect(mock.ctx.executeTool(
        request,
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'who_am_i',
        {},
      )).resolves.toBe('alice@databricks.com');

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/_apx/internal/appkit/tools/who_am_i',
        expect.objectContaining({
          headers: expect.objectContaining({
            'x-forwarded-user': 'alice@databricks.com',
            'x-forwarded-email': 'alice@databricks.com',
            'x-forwarded-access-token': 'alice-token',
          }),
        }),
      );
      expect(forwardedSignal).toBeInstanceOf(AbortSignal);

      const tokenlessRequest = createMockRequest({
        headers: { 'x-forwarded-user': 'alice@databricks.com' },
      });
      await expect(mock.ctx.executeTool(
        tokenlessRequest,
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'who_am_i',
        {},
      )).rejects.toThrow(/user token/i);
      expect(fetchMock).toHaveBeenCalledTimes(1);

      const cancellation = new AbortController();
      const cancelled = mock.ctx.executeTool(
        request,
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'who_am_i',
        {},
        cancellation.signal,
      ).then(() => {
        cancelledExecutionCompleted = true;
      });
      await inFlightFetchStarted;
      expect(cancelledExecutionCompleted).toBe(false);
      cancellation.abort();
      await expect(cancelled).rejects.toThrow(/aborted/i);
      expect(cancellationObserved).toBe(true);
      expect(cancelledExecutionCompleted).toBe(false);
      expect(forwardedSignal?.aborted).toBe(true);

      const denied = mock.ctx.executeTool(
        request,
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'apply_change',
        { value: 'deny' },
      );
      await expect(denied).rejects.toThrow(
        'APX Python bridge failed for apply_change: Tool execution is denied',
      );
      await expect(denied).rejects.not.toThrow(/approval|retry/i);

      await expect(mock.ctx.executeTool(
        request,
        INTERNAL_APX_APPKIT_PLUGIN_NAME,
        'remember',
        { value: 'never' },
      )).rejects.toThrow(
        'APX Python bridge failed for remember: '
          + 'APX AppKit bridge cannot execute stateful tool: remember',
      );
      expect(executed).toEqual(['who_am_i']);
    } finally {
      serviceContext.restore();
    }
  });

  it('uses the native AppKit approval gate for mutating tools', async () => {
    setupDatabricksEnv();
    const serviceContext = mockServiceContext({ userId: 'alice@databricks.com' });
    const executions: string[] = [];
    const adapterResults: unknown[] = [];
    const adapter: AgentAdapter = {
      async *run(_input, context) {
        yield {
          type: 'tool_call',
          callId: 'apply-1',
          name: 'apply_change',
          args: { value: 'approved' },
        };
        const result = await context.executeTool('apply_change', { value: 'approved' });
        adapterResults.push(result);
        yield { type: 'message', content: String(result) };
      },
    };
    const descriptor = agents({
      agents: {
        governed: createAgent({
          name: 'governed',
          default: true,
          instructions: 'Apply the requested governed change.',
          model: adapter,
          tools: {
            apply_change: tool({
              description: 'Apply one governed change after approval.',
              schema: z.object({ value: z.string() }),
              annotations: { effect: 'update' },
              execute: async ({ value }) => {
                executions.push(value);
                return `applied:${value}`;
              },
            }),
          },
        }),
      },
      approval: { requireForDestructive: true, timeoutMs: 1_000 },
    });
    const plugin = new descriptor.plugin(descriptor.config);
    const context = createTestPluginContext();
    const router = createMockRouter();

    try {
      await context.attach(plugin);
      await plugin.setup();
      plugin.injectRoutes(router.router);
      const chat = router.getHandler('post', '/chat');
      const approve = router.getHandler('post', '/approve');
      const invoke = context.routes.find(
        (route) => route.method === 'post' && route.path === '/invocations',
      )?.handlers[0];
      expect(chat).toBeTypeOf('function');
      expect(approve).toBeTypeOf('function');
      expect(invoke).toBeTypeOf('function');

      const nonStreamingResponse = createMockResponse();
      await invoke?.(
        createMockRequest({ obo: true, body: { input: 'apply it' } }),
        nonStreamingResponse,
        () => undefined,
      );
      expect(nonStreamingResponse.status).toHaveBeenCalledWith(400);
      expect(nonStreamingResponse.json).toHaveBeenCalledWith(expect.objectContaining({
        error: expect.stringMatching(/non-streaming and cannot run HITL/),
      }));

      const decide = async (decision: 'approve' | 'deny') => {
        const streamResponse = createMockResponse();
        const stream = chat(
          createMockRequest({
            obo: { userId: 'alice@databricks.com', token: 'alice-token' },
            body: { message: 'apply it', agent: 'governed' },
          }),
          streamResponse,
        );
        const pending = await waitForSseEvent(streamResponse, 'appkit.approval_pending');
        expect(executions).toHaveLength(0);

        const decisionResponse = createMockResponse();
        await approve(
          createMockRequest({
            obo: { userId: 'alice@databricks.com', token: 'alice-token' },
            body: {
              streamId: pending.stream_id,
              approvalId: pending.approval_id,
              decision,
            },
          }),
          decisionResponse,
        );
        expect(decisionResponse.json).toHaveBeenCalledWith({ decision });
        await stream;
        await expectStream(streamResponse).toEmit(
          'appkit.approval_pending',
          'response.completed',
        );
      };

      await decide('deny');
      expect(adapterResults).toEqual([
        'Tool execution denied by user approval gate (tool: apply_change).',
      ]);
      expect(executions).toEqual([]);

      await decide('approve');
      expect(adapterResults).toEqual([
        'Tool execution denied by user approval gate (tool: apply_change).',
        'applied:approved',
      ]);
      expect(executions).toEqual(['approved']);
    } finally {
      await plugin.shutdown();
      serviceContext.restore();
    }
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
