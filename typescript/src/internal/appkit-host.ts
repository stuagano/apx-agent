/**
 * Internal AppKit host for APX-governed agents.
 *
 * APX's external interface stays the Python/declaration layer. This module is
 * Apps deploy-target machinery: it lets Databricks AppKit `agents()` own Apps
 * routing, streaming, approval, and OBO-aware tool dispatch while APX keeps
 * policy/audit hooks around tool execution.
 */

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

export interface InternalApxAppKitGovernanceConfig extends BasePluginConfig {
  agent?: AgentExports | (() => AgentExports);
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

function resolveAgentExports(agent: AgentExports | (() => AgentExports)): AgentExports {
  return typeof agent === 'function' ? agent() : agent;
}

function requireAgentExports(agent: AgentExports | (() => AgentExports) | undefined): AgentExports {
  if (!agent) throw new Error('APX AppKit governance plugin requires an agent export');
  return resolveAgentExports(agent);
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
    return new Map(this.agentExports.getTools().map((tool) => [tool.name, tool]));
  }

  getAgentTools(): AgentToolDefinition[] {
    return this.agentExports
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

  async executeAgentTool(name: string, args: unknown): Promise<unknown> {
    const tool = this.toolMap.get(name);
    if (!tool) throw new Error(`Unknown APX tool: ${name}`);

    const event: InternalApxAppKitToolEvent = {
      toolName: name,
      args,
      annotations: toolAnnotations(tool, this.config.toolAnnotations),
    };
    const decision = (await this.config.policy?.(event)) ?? { action: 'ALLOW' };
    if (decision.action === 'DENY') {
      const reason = decision.reason ?? `APX policy denied ${name}`;
      await this.config.audit?.({ ...event, action: 'DENY', reason });
      throw new Error(reason);
    }

    try {
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
