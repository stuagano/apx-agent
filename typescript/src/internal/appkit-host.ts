/**
 * Internal AppKit host for APX-governed agents.
 *
 * APX's external interface stays the Python/declaration layer. This module is
 * Apps deploy-target machinery: it lets Databricks AppKit `agents()` own Apps
 * routing, streaming, approval, and OBO-aware tool dispatch.
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import { createHash } from 'node:crypto';

import {
  Plugin,
  ResourceType,
  toPlugin,
  type BasePluginConfig,
  type PluginManifest,
  type ResourceRequirement,
} from '@databricks/appkit';
import {
  createAgent,
  tool,
  type AgentDefinition,
  type AgentTool as AppKitAgentTool,
  type AgentToolDefinition,
  type PromptContext,
  type ToolAnnotations,
  type ToolProvider,
  type ToolkitEntry,
  type ToolkitOptions,
} from '@databricks/appkit/beta';
import { z } from 'zod';

import type { AgentConfig, AgentExports, AgentTool } from '../agent/index.js';
import { toStrictSchema, zodToJsonSchema } from '../agent/index.js';

export const INTERNAL_APX_APPKIT_PLUGIN_NAME = 'apx';

interface InternalApxAppsHostResource {
  kind: string;
  identifier: string;
}

const bridgeHeaderStorage = new AsyncLocalStorage<Record<string, string>>();
const FORWARDED_HEADER_NAMES = [
  'x-forwarded-host',
  'x-forwarded-preferred-username',
  'x-forwarded-user',
  'x-forwarded-email',
  'x-forwarded-access-token',
  'x-request-id',
] as const;
const FORWARDED_ACCESS_TOKEN_HEADER = 'x-forwarded-access-token';

export interface InternalApxAppsHostManifest {
  kind: 'apx.apps_host_manifest';
  version: 1;
  agent: {
    name: string;
    description: string;
    model: string;
    instructions: string;
    temperature: number | null;
    max_tokens: number | null;
    max_iterations: number;
  };
  appkit: {
    default: boolean;
    tool_prefix: string;
    max_steps: number;
    max_tokens: number | null;
    limits: {
      max_tool_calls: number;
      max_concurrent_streams_per_user?: number;
      max_sub_agent_depth?: number;
      tool_call_timeout_ms?: number;
    };
    ephemeral: boolean | null;
    generation_params: AgentDefinition['generationParams'] | null;
  };
  tools: Array<{
    name: string;
    description: string;
    runtime: 'python';
    parameters: Record<string, unknown>;
    output_schema: Record<string, unknown> | null;
    annotations: {
      effect: NonNullable<ToolAnnotations['effect']>;
      execution_identity: 'user' | 'service';
      requires_request_context: boolean;
      requires_user_context: boolean;
    };
    handler: {
      kind: 'python';
      ref: string;
    };
    resources: InternalApxAppsHostResource[];
    user_api_scopes: string[];
  }>;
  resources: InternalApxAppsHostResource[];
  user_resources: InternalApxAppsHostResource[];
  service_resources: InternalApxAppsHostResource[];
  user_api_scopes: string[];
  app_to_app_permissions: Array<{
    url: string;
    permission: 'CAN_USE';
  }>;
}

export interface InternalApxAppKitGovernanceConfig extends BasePluginConfig {
  agent?: AgentExports | (() => AgentExports);
  manifest?: InternalApxAppsHostManifest;
  pythonBridge?: {
    baseUrl: string;
    headers?: Record<string, string>;
  };
  toolAnnotations?: Record<string, ToolAnnotations>;
}

export interface InternalApxAppKitAgentOptions {
  default?: boolean;
  baseSystemPrompt?: AgentDefinition['baseSystemPrompt'];
  generationParams?: AgentDefinition['generationParams'];
  maxSteps?: number;
  maxTokens?: number;
  ephemeral?: boolean;
  toolPrefix?: string;
}

export interface InternalApxAppKitAgentsOptions {
  [key: string]: unknown;
  approval: {
    requireForDestructive: boolean;
  };
  limits: {
    maxToolCalls?: number;
    maxConcurrentStreamsPerUser?: number;
    maxSubAgentDepth?: number;
    toolCallTimeoutMs?: number;
  };
}

export interface InternalApxAppKitDevSkill {
  name: string;
  description: string;
  content: string;
}

export interface InternalApxAppKitDevSnapshot {
  agentName: string;
  model: string;
  originalModel: string;
  instructions: string;
  instructionsOverridden: boolean;
  tools: Array<{
    name: string;
    description: string;
    enabled: boolean;
    annotations: ToolAnnotations;
  }>;
  skills: InternalApxAppKitDevSkill[];
  systemPrompt: string;
  overridesEphemeral: true;
}

export interface InternalApxAppKitDevRuntime {
  snapshot(): InternalApxAppKitDevSnapshot;
  definition(): AgentDefinition;
  setModel(model: string): void;
  setInstructions(instructions: string | null): void;
  setToolEnabled(name: string, enabled: boolean): void;
  setSkill(skill: InternalApxAppKitDevSkill): void;
  deleteSkill(name: string): boolean;
}

const devModelSchema = z.string().trim().min(1).max(256);
const devInstructionsSchema = z.string().max(64_000);
const devSkillSchema = z.object({
  name: z.string().trim().min(1).max(64).regex(/^[A-Za-z0-9_-]+$/),
  description: z.string().max(500),
  content: z.string().min(1).max(20_000),
});

function internalApxAppKitBaseSystemPrompt(context: PromptContext): string {
  const lines = ['You are an AI assistant running on Databricks AppKit.'];
  if (context.pluginNames.length > 0) {
    lines.push('', `Active AppKit plugins: ${context.pluginNames.join(', ')}`);
  }
  lines.push(
    '',
    'Guidelines:',
    '- Be concise: for large or noisy tool output, summarize what matters and how to go deeper instead of pasting everything.',
    '- Use each tool as defined: pass required arguments and use the syntax, dialect, or path rules the target system expects (see each tool’s description and schema).',
    '- If a tool call fails, explain the error in plain language and suggest a fix or next step.',
    '- Respect tool metadata and app policy: read-only vs destructive tools, user/identity context, and any approval or safety flows the app provides.',
  );
  return lines.join('\n');
}

export function internalApxAppKitSystemPrompt(
  instructions: string,
  context: PromptContext,
): string {
  const base = internalApxAppKitBaseSystemPrompt(context);
  return instructions ? `${base}\n\n${instructions}` : base;
}

function resolveAgentExports(agent: AgentExports | (() => AgentExports)): AgentExports {
  return typeof agent === 'function' ? agent() : agent;
}

function requireAgentExports(agent: AgentExports | (() => AgentExports) | undefined): AgentExports {
  if (!agent) throw new Error('APX AppKit governance plugin requires an agent export');
  return resolveAgentExports(agent);
}

function requireAgentSource(config: InternalApxAppKitGovernanceConfig):
  | { kind: 'exports'; value: AgentExports }
  | { kind: 'manifest'; value: InternalApxAppsHostManifest } {
  if (config.manifest) return { kind: 'manifest', value: config.manifest };
  return { kind: 'exports', value: requireAgentExports(config.agent) };
}

function bridgeHeadersFromRequest(
  req: Parameters<Plugin['asUser']>[0],
  identity: 'user' | 'service',
): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const name of FORWARDED_HEADER_NAMES) {
    if (identity === 'service' && name === FORWARDED_ACCESS_TOKEN_HEADER) continue;
    const value = req.header(name)?.trim();
    if (value) headers[name] = value;
  }
  return headers;
}

function bridgeConfigHeaders(
  headers: Record<string, string> | undefined,
  identity: 'user' | 'service',
): Record<string, string> {
  if (identity === 'user') return headers ?? {};
  return Object.fromEntries(
    Object.entries(headers ?? {}).filter(
      ([name]) => name.toLowerCase() !== FORWARDED_ACCESS_TOKEN_HEADER,
    ),
  );
}

function manifestToolExecutionIdentity(
  tool: NonNullable<InternalApxAppsHostManifest['tools']>[number] | undefined,
): 'user' | 'service' {
  if (tool?.annotations?.execution_identity) return tool.annotations.execution_identity;
  return tool?.annotations?.requires_user_context === false ? 'service' : 'user';
}

function appKitResourceRequirement(
  resource: InternalApxAppsHostResource,
): ResourceRequirement {
  const resourceKey = `apx-${resource.kind.replaceAll('_', '-')}-${createHash('sha256')
    .update(`${resource.kind}:${resource.identifier}`)
    .digest('hex')
    .slice(0, 8)}`;
  const base = {
    alias: `${resource.kind}: ${resource.identifier}`,
    resourceKey,
    description: `APX-declared ${resource.kind} resource`,
    required: true,
  };
  const field = (name: string) => ({ [name]: { value: resource.identifier } });

  switch (resource.kind) {
    case 'job':
      return { ...base, type: ResourceType.JOB, permission: 'CAN_MANAGE_RUN', fields: field('id') };
    case 'serving_endpoint':
      return { ...base, type: ResourceType.SERVING_ENDPOINT, permission: 'CAN_QUERY', fields: field('name') };
    case 'sql_warehouse':
      return { ...base, type: ResourceType.SQL_WAREHOUSE, permission: 'CAN_USE', fields: field('id') };
    case 'vector_search_index':
      return { ...base, type: ResourceType.VECTOR_SEARCH_INDEX, permission: 'SELECT', fields: field('name') };
    case 'uc_function':
      return { ...base, type: ResourceType.UC_FUNCTION, permission: 'EXECUTE', fields: field('name') };
    case 'uc_connection':
      return { ...base, type: ResourceType.UC_CONNECTION, permission: 'USE_CONNECTION', fields: field('name') };
    case 'genie_space':
      return { ...base, type: ResourceType.GENIE_SPACE, permission: 'CAN_RUN', fields: field('id') };
    case 'lakebase_instance':
      return {
        ...base,
        type: ResourceType.DATABASE,
        permission: 'CAN_CONNECT_AND_CREATE',
        fields: {
          instance_name: { value: resource.identifier },
          database_name: { value: 'databricks_postgres' },
        },
      };
    case 'app':
      return { ...base, type: ResourceType.APP, permission: 'CAN_USE', fields: field('name') };
    default:
      throw new Error(`Unsupported APX AppKit service resource kind: ${resource.kind}`);
  }
}

function toolAnnotations(
  tool: AgentTool,
  overrides: Record<string, ToolAnnotations> | undefined,
): ToolAnnotations {
  return {
    effect: 'update',
    requiresUserContext: true,
    ...overrides?.[tool.name],
  };
}

function manifestToolAnnotations(
  tool: NonNullable<InternalApxAppsHostManifest['tools']>[number],
): ToolAnnotations {
  return {
    effect: tool.annotations?.effect ?? 'update',
    requiresUserContext: manifestToolExecutionIdentity(tool) === 'user',
  };
}

function toAppKitToolDefinition(
  tool: AgentTool,
  overrides: Record<string, ToolAnnotations> | undefined,
): AgentToolDefinition {
  return {
    name: tool.name,
    description: tool.description,
    parameters: toStrictSchema(zodToJsonSchema(tool.parameters)),
    annotations: toolAnnotations(tool, overrides),
  };
}

function toAppKitManifestToolDefinition(
  tool: NonNullable<InternalApxAppsHostManifest['tools']>[number],
): AgentToolDefinition {
  return {
    name: tool.name,
    description: tool.description ?? '',
    parameters: toStrictSchema(tool.parameters),
    annotations: manifestToolAnnotations(tool),
  };
}

function filterToolDefinitions(
  defs: AgentToolDefinition[],
  opts: ToolkitOptions,
): AgentToolDefinition[] {
  const only = opts.only ? new Set(opts.only) : null;
  const except = new Set(opts.except ?? []);
  return defs.filter((def) => (!only || only.has(def.name)) && !except.has(def.name));
}

export class InternalApxAppKitGovernancePlugin
  extends Plugin<InternalApxAppKitGovernanceConfig>
  implements ToolProvider
{
  static manifest = {
    name: INTERNAL_APX_APPKIT_PLUGIN_NAME,
    displayName: 'APX Governance',
    description: 'Internal APX governed agent declarations exposed as AppKit agent tools',
    resources: { required: [], optional: [] },
  } satisfies PluginManifest<typeof INTERNAL_APX_APPKIT_PLUGIN_NAME>;

  static getResourceRequirements(
    config: InternalApxAppKitGovernanceConfig,
  ): ResourceRequirement[] {
    return (config.manifest?.service_resources ?? []).map(appKitResourceRequirement);
  }

  private get agentExports(): AgentExports {
    return requireAgentExports(this.config.agent);
  }

  private get toolMap(): Map<string, AgentTool> {
    const source = requireAgentSource(this.config);
    if (source.kind === 'manifest') return new Map();
    return new Map(source.value.getTools().map((tool) => [tool.name, tool]));
  }

  asUser(req: Parameters<Plugin['asUser']>[0]): this {
    const executeAsUser = (name: string, args: unknown, signal?: AbortSignal) => (
      super.asUser(req).executeAgentTool(name, args, signal)
    );
    return new Proxy(this, {
      get(target, prop, receiver) {
        const value = Reflect.get(target, prop, receiver);
        if (prop !== 'executeAgentTool' || typeof value !== 'function') return value;
        return (name: string, args: unknown, signal?: AbortSignal) => {
          const manifestTool = target.config.manifest?.tools?.find(
            (candidate) => candidate.name === name,
          );
          const identity = manifestToolExecutionIdentity(manifestTool);
          const invoke = () => (
            identity === 'user'
              ? executeAsUser(name, args, signal)
              : target.executeAgentTool(name, args, signal)
          );
          const needsRequest = identity === 'user'
            || manifestTool?.annotations?.requires_request_context === true;
          return needsRequest
            ? bridgeHeaderStorage.run(bridgeHeadersFromRequest(req, identity), invoke)
            : invoke();
        };
      },
    });
  }

  getAgentTools(): AgentToolDefinition[] {
    const source = requireAgentSource(this.config);
    if (source.kind === 'manifest') {
      return (source.value.tools ?? []).map((tool) => toAppKitManifestToolDefinition(tool));
    }
    return source.value
      .getTools()
      .map((tool) => toAppKitToolDefinition(tool, this.config.toolAnnotations));
  }

  toolkit(opts: ToolkitOptions = {}): Record<string, ToolkitEntry> {
    const prefix = opts.prefix ?? `${INTERNAL_APX_APPKIT_PLUGIN_NAME}.`;
    const entries: Record<string, ToolkitEntry> = {};

    for (const def of filterToolDefinitions(this.getAgentTools(), opts)) {
      const key = opts.rename?.[def.name] ?? `${prefix}${def.name}`;
      entries[key] = {
        __toolkitRef: true,
        pluginName: INTERNAL_APX_APPKIT_PLUGIN_NAME,
        localName: def.name,
        def,
        annotations: def.annotations,
        autoInheritable: def.annotations?.effect === 'read',
      };
    }

    return entries;
  }

  async executeAgentTool(name: string, args: unknown, signal?: AbortSignal): Promise<unknown> {
    const tool = this.toolMap.get(name);
    const manifestTool = this.config.manifest?.tools?.find((candidate) => candidate.name === name);
    if (!tool && !manifestTool) throw new Error(`Unknown APX tool: ${name}`);

    if (!tool) {
      const bridge = this.config.pythonBridge;
      if (!bridge) throw new Error(`APX Python bridge is not configured for tool: ${name}`);
      const identity = manifestToolExecutionIdentity(manifestTool);
      const response = await fetch(
        `${bridge.baseUrl.replace(/\/$/, '')}/_apx/internal/appkit/tools/${encodeURIComponent(name)}`,
        {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            ...bridgeConfigHeaders(bridge.headers, identity),
            ...bridgeHeaderStorage.getStore(),
          },
          body: JSON.stringify({ args }),
          signal,
        },
      );
      if (!response.ok) {
        let detail = `${response.status} ${response.statusText}`;
        try {
          const payload = await response.json();
          detail = typeof payload?.detail === 'string' ? payload.detail : detail;
        } catch {
          // Keep the status text when the bridge returns a non-JSON error.
        }
        throw new Error(`APX Python bridge failed for ${name}: ${detail}`);
      }
      const payload = await response.json();
      return payload.result;
    }
    return tool.handler(args);
  }
}

export const internalApxAppKitGovernance = toPlugin(InternalApxAppKitGovernancePlugin);

export function internalApxAppKitAgentsOptionsFromManifest(
  manifest: InternalApxAppsHostManifest,
): InternalApxAppKitAgentsOptions {
  const limits = manifest.appkit?.limits ?? {};
  return {
    approval: { requireForDestructive: true },
    limits: {
      maxToolCalls: limits.max_tool_calls ?? manifest.appkit?.max_steps ?? manifest.agent.max_iterations,
      maxConcurrentStreamsPerUser: limits.max_concurrent_streams_per_user,
      maxSubAgentDepth: limits.max_sub_agent_depth,
      toolCallTimeoutMs: limits.tool_call_timeout_ms,
    },
  };
}

export function createInternalApxAppKitAgentDefinition(
  agent: AgentExports | (() => AgentExports),
  options: InternalApxAppKitAgentOptions = {},
): AgentDefinition {
  const exports = resolveAgentExports(agent);
  const config: AgentConfig = exports.getConfig();
  return createAgent({
    name: config.name,
    instructions: config.instructions ?? '',
    model: config.model,
    default: options.default,
    baseSystemPrompt: options.baseSystemPrompt,
    generationParams: options.generationParams,
    maxSteps: options.maxSteps ?? config.maxIterations,
    maxTokens: options.maxTokens,
    ephemeral: options.ephemeral,
    tools(plugins) {
      return {
        ...plugins[INTERNAL_APX_APPKIT_PLUGIN_NAME].toolkit({
          prefix: options.toolPrefix ?? `${INTERNAL_APX_APPKIT_PLUGIN_NAME}.`,
        }),
      };
    },
  });
}

export function createInternalApxAppKitAgentDefinitionFromManifest(
  manifest: InternalApxAppsHostManifest,
  options: InternalApxAppKitAgentOptions = {},
): AgentDefinition {
  return createAgent({
    name: manifest.agent.name,
    instructions: manifest.agent.instructions ?? '',
    model: manifest.agent.model,
    default: options.default ?? manifest.appkit?.default,
    baseSystemPrompt: options.baseSystemPrompt,
    generationParams: options.generationParams ?? manifest.appkit?.generation_params ?? undefined,
    maxSteps: options.maxSteps ?? manifest.appkit?.max_steps ?? manifest.agent.max_iterations,
    maxTokens: options.maxTokens ?? manifest.appkit?.max_tokens ?? manifest.agent.max_tokens ?? undefined,
    ephemeral: options.ephemeral ?? manifest.appkit?.ephemeral ?? undefined,
    tools(plugins) {
      return {
        ...plugins[INTERNAL_APX_APPKIT_PLUGIN_NAME].toolkit({
          prefix: options.toolPrefix ?? manifest.appkit?.tool_prefix ?? `${INTERNAL_APX_APPKIT_PLUGIN_NAME}.`,
        }),
      };
    },
  });
}

export function createInternalApxAppKitDevRuntime(
  manifest: InternalApxAppsHostManifest,
): InternalApxAppKitDevRuntime {
  let model = manifest.agent.model;
  let instructionsOverride: string | null = null;
  const enabledTools = new Set((manifest.tools ?? []).map((candidate) => candidate.name));
  const skills = new Map<string, InternalApxAppKitDevSkill>();
  const prefix = manifest.appkit?.tool_prefix ?? `${INTERNAL_APX_APPKIT_PLUGIN_NAME}.`;

  const instructions = () => instructionsOverride ?? manifest.agent.instructions ?? '';
  const promptContext = (): PromptContext => ({
    agentName: manifest.agent.name,
    pluginNames: [INTERNAL_APX_APPKIT_PLUGIN_NAME],
    toolNames: [
      ...(manifest.tools ?? [])
        .filter((candidate) => enabledTools.has(candidate.name))
        .map((candidate) => `${prefix}${candidate.name}`),
      ...[...skills.keys()].map((name) => `skill.${name}`),
    ],
  });

  return {
    snapshot() {
      return {
        agentName: manifest.agent.name,
        model,
        originalModel: manifest.agent.model,
        instructions: instructions(),
        instructionsOverridden: instructionsOverride !== null,
        tools: (manifest.tools ?? []).map((candidate) => ({
          name: candidate.name,
          description: candidate.description ?? '',
          enabled: enabledTools.has(candidate.name),
          annotations: manifestToolAnnotations(candidate),
        })),
        skills: [...skills.values()],
        systemPrompt: internalApxAppKitSystemPrompt(instructions(), promptContext()),
        overridesEphemeral: true,
      };
    },
    definition() {
      const baseSystemPrompt = internalApxAppKitSystemPrompt('', promptContext());
      return createAgent({
        name: manifest.agent.name,
        instructions: instructions(),
        model,
        default: manifest.appkit?.default,
        baseSystemPrompt,
        generationParams: manifest.appkit?.generation_params ?? undefined,
        maxSteps: manifest.appkit?.max_steps ?? manifest.agent.max_iterations,
        maxTokens: manifest.appkit?.max_tokens ?? manifest.agent.max_tokens ?? undefined,
        ephemeral: manifest.appkit?.ephemeral ?? undefined,
        tools(plugins) {
          const skillTools: Record<string, AppKitAgentTool> = {};
          for (const skill of skills.values()) {
            skillTools[`skill.${skill.name}`] = tool({
              description: skill.description,
              schema: z.object({}),
              annotations: { effect: 'read', requiresUserContext: false },
              execute: async () => skill.content,
            });
          }
          return {
            ...plugins[INTERNAL_APX_APPKIT_PLUGIN_NAME].toolkit({
              prefix,
              only: [...enabledTools],
            }),
            ...skillTools,
          };
        },
      });
    },
    setModel(nextModel) {
      model = devModelSchema.parse(nextModel);
    },
    setInstructions(nextInstructions) {
      instructionsOverride = nextInstructions === null
        ? null
        : devInstructionsSchema.parse(nextInstructions);
    },
    setToolEnabled(name, enabled) {
      if (!(manifest.tools ?? []).some((candidate) => candidate.name === name)) {
        throw new Error(`Unknown APX tool: ${name}`);
      }
      if (enabled) enabledTools.add(name);
      else enabledTools.delete(name);
    },
    setSkill(skill) {
      const parsed = devSkillSchema.parse(skill);
      skills.set(parsed.name, parsed);
    },
    deleteSkill(name) {
      return skills.delete(name);
    },
  };
}
