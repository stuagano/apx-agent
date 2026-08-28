/**
 * Internal AppKit host for APX-governed agents.
 *
 * APX's external interface stays the Python/declaration layer. This module is
 * Apps deploy-target machinery: it lets Databricks AppKit `agents()` own Apps
 * routing, streaming, approval, and OBO-aware tool dispatch while APX keeps
 * policy/audit hooks around tool execution.
 */

import { AsyncLocalStorage } from 'node:async_hooks';

import {
  Plugin,
  toPlugin,
  type BasePluginConfig,
  type PluginManifest,
} from '@databricks/appkit';
import {
  createAgent,
  type AgentDefinition,
  type AgentToolDefinition,
  type ToolAnnotations,
  type ToolProvider,
  type ToolkitEntry,
  type ToolkitOptions,
} from '@databricks/appkit/beta';

import type { AgentConfig, AgentExports, AgentTool } from '../agent/index.js';
import { toStrictSchema, zodToJsonSchema } from '../agent/index.js';

export const INTERNAL_APX_APPKIT_PLUGIN_NAME = 'apx';

const bridgeHeaderStorage = new AsyncLocalStorage<Record<string, string>>();
const FORWARDED_HEADER_NAMES = [
  'x-forwarded-host',
  'x-forwarded-preferred-username',
  'x-forwarded-user',
  'x-forwarded-email',
  'x-forwarded-access-token',
  'x-request-id',
] as const;

export type InternalApxAppKitPolicyAction = 'ALLOW' | 'DENY';

export interface InternalApxAppKitToolEvent {
  toolName: string;
  args: unknown;
  annotations?: ToolAnnotations;
}

export interface InternalApxAppKitAuditEvent extends InternalApxAppKitToolEvent {
  action: InternalApxAppKitPolicyAction;
  reason: string | null;
  error?: string;
}

export interface InternalApxAppKitPolicyDecision {
  action: InternalApxAppKitPolicyAction;
  reason?: string | null;
}

export interface InternalApxAppsHostManifest {
  agent: {
    name: string;
    model: string;
    instructions?: string;
    max_iterations?: number;
    max_tokens?: number | null;
  };
  appkit?: {
    default?: boolean;
    tool_prefix?: string;
    max_steps?: number;
    max_tokens?: number | null;
    limits?: {
      max_tool_calls?: number;
      max_concurrent_streams_per_user?: number;
      max_sub_agent_depth?: number;
      tool_call_timeout_ms?: number;
    };
    ephemeral?: boolean | null;
    generation_params?: AgentDefinition['generationParams'] | null;
  };
  tools?: Array<{
    name: string;
    description?: string;
    parameters: Record<string, unknown>;
    annotations?: {
      effect?: ToolAnnotations['effect'];
      requires_user_context?: boolean;
    };
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
  policy?: (
    event: InternalApxAppKitToolEvent,
  ) => InternalApxAppKitPolicyDecision | Promise<InternalApxAppKitPolicyDecision>;
  audit?: (event: InternalApxAppKitAuditEvent) => void | Promise<void>;
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

function bridgeHeadersFromRequest(req: Parameters<Plugin['asUser']>[0]): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const name of FORWARDED_HEADER_NAMES) {
    const value = req.header(name)?.trim();
    if (value) headers[name] = value;
  }
  return headers;
}

function toolAnnotations(
  tool: AgentTool,
  overrides: Record<string, ToolAnnotations> | undefined,
): ToolAnnotations {
  return {
    effect: 'read',
    requiresUserContext: true,
    ...overrides?.[tool.name],
  };
}

function manifestToolAnnotations(
  tool: NonNullable<InternalApxAppsHostManifest['tools']>[number],
): ToolAnnotations {
  return {
    effect: tool.annotations?.effect ?? 'read',
    requiresUserContext: tool.annotations?.requires_user_context ?? true,
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

  private get agentExports(): AgentExports {
    return requireAgentExports(this.config.agent);
  }

  private get toolMap(): Map<string, AgentTool> {
    const source = requireAgentSource(this.config);
    if (source.kind === 'manifest') return new Map();
    return new Map(source.value.getTools().map((tool) => [tool.name, tool]));
  }

  asUser(req: Parameters<Plugin['asUser']>[0]): this {
    const scoped = super.asUser(req);
    const headers = bridgeHeadersFromRequest(req);
    return new Proxy(scoped, {
      get(target, prop, receiver) {
        const value = Reflect.get(target, prop, receiver);
        if (typeof value !== 'function') return value;
        return (...args: unknown[]) => bridgeHeaderStorage.run(headers, () => value.apply(target, args));
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

    const event: InternalApxAppKitToolEvent = {
      toolName: name,
      args,
      annotations: tool
        ? toolAnnotations(tool, this.config.toolAnnotations)
        : manifestToolAnnotations(manifestTool!),
    };
    const decision = (await this.config.policy?.(event)) ?? { action: 'ALLOW' };
    if (decision.action === 'DENY') {
      const reason = decision.reason ?? `APX policy denied ${name}`;
      await this.config.audit?.({ ...event, action: 'DENY', reason });
      throw new Error(reason);
    }

    try {
      if (!tool) {
        const bridge = this.config.pythonBridge;
        if (!bridge) throw new Error(`APX Python bridge is not configured for tool: ${name}`);
        const response = await fetch(
          `${bridge.baseUrl.replace(/\/$/, '')}/_apx/internal/appkit/tools/${encodeURIComponent(name)}`,
          {
            method: 'POST',
            headers: {
              'content-type': 'application/json',
              ...bridge.headers,
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
        await this.config.audit?.({ ...event, action: 'ALLOW', reason: null });
        return payload.result;
      }
      const result = await tool.handler(args);
      await this.config.audit?.({ ...event, action: 'ALLOW', reason: null });
      return result;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.config.audit?.({
        ...event,
        action: 'ALLOW',
        reason: null,
        error: message,
      });
      throw error;
    }
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
