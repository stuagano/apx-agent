/**
 * apx info — introspect an agent (instructions, tools, sub-agents, resources).
 *
 * Pure local — no Databricks calls. Useful as a sanity check before deploy
 * and as a programmatic source of truth for what an agent declares.
 *
 * Bridges to:
 *   - {@link loadAgent}            — resolve `module:variable` to an object.
 *   - {@link collectResourceSpecs} — same walker used by deploy/log to compute
 *     the resources= list. Uses default iterators that read `.tools` /
 *     `.subAgents` off the agent config, mirroring `lint.ts`'s `unwrapAgent`.
 *
 * # Caveat — composite agents
 *
 * The default iterators don't recurse into Sequential / Parallel / Loop /
 * Router / Handoff agents (TS port doesn't have a uniform "walk all
 * sub-trees" helper yet). For composite agents, the listed tools / resources
 * may underreport. The Python CLI walks the full tree via
 * `_iter_tool_fns`/`_iter_sub_agents`; the TS port matches the simpler
 * lint.ts walk for now.
 */

import type { Command } from 'commander';
import { z } from 'zod';

import { loadAgent } from '../load-agent.js';
import { collectResourceSpecs, getResources, type ResourceSpec } from '../../resources.js';

interface AgentLike {
  readonly instructions?: string;
  readonly tools?: ReadonlyArray<unknown>;
  readonly subAgents?: ReadonlyArray<string>;
  readonly model?: string;
}

interface ToolLike {
  readonly name?: string;
  readonly description?: string;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

/**
 * Resolve any of the supported agent shapes (plugin, AgentExports, plain
 * AgentConfig) into a uniform `AgentLike`. Mirrors `unwrapAgent` in lint.ts.
 */
function unwrapAgent(value: unknown): AgentLike | null {
  if (!isPlainObject(value)) return null;

  if (typeof value.exports === 'function') {
    try {
      const exports = (value.exports as () => unknown)();
      if (isPlainObject(exports) && typeof exports.getConfig === 'function') {
        const cfg = (exports.getConfig as () => unknown)();
        const tools =
          typeof exports.getTools === 'function'
            ? ((exports.getTools as () => unknown)() as unknown[])
            : undefined;
        return mergeConfigAndTools(cfg, tools);
      }
    } catch {
      // fall through
    }
  }

  if (typeof value.getConfig === 'function') {
    const cfg = (value.getConfig as () => unknown)();
    const tools =
      typeof value.getTools === 'function'
        ? ((value.getTools as () => unknown)() as unknown[])
        : undefined;
    return mergeConfigAndTools(cfg, tools);
  }

  return toAgentLike(value);
}

function mergeConfigAndTools(cfg: unknown, toolsOverride: unknown): AgentLike | null {
  const base = toAgentLike(cfg);
  if (!base) return null;
  if (Array.isArray(toolsOverride)) return { ...base, tools: toolsOverride };
  return base;
}

function toAgentLike(value: unknown): AgentLike | null {
  if (!isPlainObject(value)) return null;
  return {
    instructions: typeof value.instructions === 'string' ? value.instructions : undefined,
    tools: Array.isArray(value.tools) ? (value.tools as unknown[]) : undefined,
    subAgents: Array.isArray(value.subAgents)
      ? (value.subAgents as unknown[]).filter((s): s is string => typeof s === 'string')
      : undefined,
    model: typeof value.model === 'string' ? value.model : undefined,
  };
}

function toolDoc(tool: unknown): string {
  if (!isPlainObject(tool)) return '';
  const desc = (tool as ToolLike).description;
  if (typeof desc !== 'string') return '';
  const firstLine = desc.split(/\r?\n/).find((l) => l.trim().length > 0) ?? '';
  return firstLine.trim();
}

function toolName(tool: unknown, fallback: string): string {
  if (isPlainObject(tool) && typeof (tool as ToolLike).name === 'string') {
    return (tool as ToolLike).name!;
  }
  return fallback;
}

interface InfoPayload {
  module: string;
  instructions: string;
  tools: Array<{
    name: string;
    description: string;
    resources: Array<{ kind: string; identifier: string }>;
  }>;
  sub_agents: string[];
  resources: Array<{ kind: string; identifier: string }>;
}

function buildPayload(agent: unknown, moduleSpec: string): InfoPayload {
  const unwrapped = unwrapAgent(agent) ?? {};
  const tools = unwrapped.tools ?? [];
  const subAgents = [...(unwrapped.subAgents ?? [])];
  const instructions = unwrapped.instructions ?? '';

  const toolsInfo = tools.map((t, idx) => ({
    name: toolName(t, `<tool ${idx}>`),
    description: toolDoc(t),
    resources: (typeof t === 'object' && t !== null ? getResources(t as object) : []).map((s) => ({
      kind: s.kind,
      identifier: s.identifier,
    })),
  }));

  const resourceSpecs: ResourceSpec[] = collectResourceSpecs<AgentLike>({
    agent: unwrapped,
    toolIterator: (a) => a.tools ?? [],
    subAgentIterator: (a) => a.subAgents ?? [],
  });

  return {
    module: moduleSpec,
    instructions,
    tools: toolsInfo,
    sub_agents: subAgents,
    resources: resourceSpecs.map((s) => ({ kind: s.kind, identifier: s.identifier })),
  };
}

function renderText(payload: InfoPayload): string {
  const lines: string[] = [];
  lines.push(`Agent loaded from ${payload.module}`);
  if (payload.instructions) {
    lines.push('');
    lines.push('Instructions:');
    lines.push(`  ${payload.instructions}`);
  }
  lines.push('');
  lines.push(`Tools (${payload.tools.length}):`);
  for (const t of payload.tools) {
    lines.push(`  - ${t.name}`);
    if (t.description) lines.push(`      ${t.description}`);
  }
  if (payload.sub_agents.length > 0) {
    lines.push('');
    lines.push(`Sub-agents (${payload.sub_agents.length}):`);
    for (const s of payload.sub_agents) {
      lines.push(`  - ${s}`);
    }
  }
  lines.push('');
  lines.push(`Declared resources (${payload.resources.length}):`);
  for (const r of payload.resources) {
    lines.push(`  - ${r.kind.padEnd(24)} ${r.identifier}`);
  }
  return lines.join('\n');
}

const InfoOptionsSchema = z.object({
  module: z.string().min(1),
  format: z.enum(['text', 'json']).default('text'),
});

export interface InfoCommandDeps {
  /** Override for tests — defaults to the real {@link loadAgent}. */
  loadAgent?: (spec: string) => Promise<unknown>;
}

export function registerInfoCommand(program: Command, deps: InfoCommandDeps = {}): void {
  const load = deps.loadAgent ?? loadAgent;
  program
    .command('info')
    .description('Introspect an agent — tools, sub-agents, declared resources.')
    .requiredOption('--module <spec>', 'Agent module spec, "module:variable".', 'agent:agent')
    .option('--format <fmt>', 'Output format (text|json).', 'text')
    .action(async (rawOpts: { module: string; format: string }) => {
      const opts = InfoOptionsSchema.parse(rawOpts);
      const agent = await load(opts.module);
      const payload = buildPayload(agent, opts.module);
      if (opts.format === 'json') {
        // eslint-disable-next-line no-console
        console.log(JSON.stringify(payload, null, 2));
      } else {
        // eslint-disable-next-line no-console
        console.log(renderText(payload));
      }
    });
}
