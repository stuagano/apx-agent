/**
 * Static checks for apx-agent agents (TypeScript port of `apx_agent._lint`).
 *
 * Catches common footguns at definition time: agents with no system prompt,
 * tools missing descriptions (the LLM has no metadata to choose from),
 * sub-agent URLs that reference unset env vars, model endpoint names that
 * don't look like Databricks Model Serving endpoints.
 *
 * Linting is best-effort and static — it can only see what's wired into the
 * agent config at lint time.
 *
 * # Rule catalog (stable codes; severities may tighten over time)
 *
 * | Code | Severity | What |
 * |------|----------|------|
 * | L001 | error    | LlmAgent has no `instructions` set. |
 * | L002 | warning  | Tool has no description — LLM can't pick it reliably. |
 * | L003 | error    | Sub-agent URL references `$ENV_VAR` that isn't set. |
 * | L004 | warning  | Model endpoint name doesn't look like a Databricks endpoint. |
 *
 * # Input shapes the default iterator handles
 *
 * TS agent types differ from Python's. The default `agentIterator` accepts:
 *   - The object returned by `createAgentPlugin({...})` (calls `.exports()`).
 *   - An `AgentExports` value (calls `.getConfig()`).
 *   - A plain object with `.instructions`, `.tools`, `.subAgents`, `.model`
 *     (an `AgentConfig`-shaped value).
 *
 * Composite/nested agents are not walked automatically — pass a custom
 * `opts.agentIterator` when you need to flatten a tree of sub-agents.
 *
 * # Deviation from Python
 *
 * - `LintFinding.code` → `rule` (per spec), plus optional `agentName` /
 *   `toolName` (so callers don't have to parse a stringly-typed `location`).
 *   The Python `location` string isn't carried — callers compose their own
 *   from `rule`/`agentName`/`toolName`/`message`.
 * - L004 allow-list mirrors Python exactly (`databricks-`, `endpoints/`).
 *   The task spec mentioned a broader list, but the required test cases and
 *   the Python parity tests both assume the narrower list.
 */
import { z } from 'zod';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export const Severity = {
  Error: 'error',
  Warning: 'warning',
  Info: 'info',
} as const;
export type Severity = (typeof Severity)[keyof typeof Severity];

export interface LintFinding {
  readonly rule: string;
  readonly severity: Severity;
  readonly message: string;
  readonly agentName?: string;
  readonly toolName?: string;
}

// ---------------------------------------------------------------------------
// Loosely-typed shapes the default iterator unwraps. Everything is `unknown`
// behind the scenes — we never trust the input.
// ---------------------------------------------------------------------------

/** Minimal AgentConfig-shaped view the linter cares about. */
export interface LintableAgent {
  readonly name?: string;
  readonly instructions?: string;
  readonly tools?: ReadonlyArray<LintableTool>;
  readonly subAgents?: ReadonlyArray<string>;
  readonly model?: string;
}

/** Minimal AgentTool-shaped view — only the fields L002 reads. */
export interface LintableTool {
  readonly name?: string;
  readonly description?: string;
}

export interface LintAgentOptions {
  /**
   * Model endpoint that will be used at deploy time. When supplied, L004
   * lints the name; otherwise only `.model` already on the agent value is
   * checked.
   */
  model?: string;
  /**
   * Override the default agent-walking strategy. Yield each agent in the
   * tree exactly once. Useful for composite agents.
   */
  agentIterator?: (root: unknown) => Iterable<unknown>;
}

// Validate only the serialisable bits with Zod — function values pass through
// untouched (Zod's function schemas have ergonomic warts in v4 that aren't
// worth the call-site cost here).
const LintAgentOptionsSchema = z.object({
  model: z.string().optional(),
  agentIterator: z
    .custom<(root: unknown) => Iterable<unknown>>(
      (v) => v === undefined || typeof v === 'function',
      { message: 'agentIterator must be a function' },
    )
    .optional(),
});

// ---------------------------------------------------------------------------
// Default agent iterator — unwraps the common TS agent shapes.
// ---------------------------------------------------------------------------

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function unwrapAgent(value: unknown): LintableAgent | null {
  if (!isPlainObject(value)) return null;

  // `createAgentPlugin(...)` returns a plugin object with `.exports()`.
  if (typeof value.exports === 'function') {
    try {
      const exports = (value.exports as () => unknown)();
      if (isPlainObject(exports) && typeof exports.getConfig === 'function') {
        const cfg = (exports.getConfig as () => unknown)();
        const tools = typeof exports.getTools === 'function'
          ? ((exports.getTools as () => unknown)() as unknown)
          : undefined;
        return mergeConfigAndTools(cfg, tools);
      }
    } catch {
      // fall through to other shapes
    }
  }

  // Bare AgentExports value.
  if (typeof value.getConfig === 'function') {
    const cfg = (value.getConfig as () => unknown)();
    const tools = typeof value.getTools === 'function'
      ? ((value.getTools as () => unknown)() as unknown)
      : undefined;
    return mergeConfigAndTools(cfg, tools);
  }

  // Plain `AgentConfig`-shaped object.
  return toLintableAgent(value);
}

function mergeConfigAndTools(cfg: unknown, toolsOverride: unknown): LintableAgent | null {
  const base = toLintableAgent(cfg);
  if (!base) return null;
  if (Array.isArray(toolsOverride)) {
    return { ...base, tools: toolsOverride.map(toLintableTool) };
  }
  return base;
}

function toLintableAgent(value: unknown): LintableAgent | null {
  if (!isPlainObject(value)) return null;
  const tools = Array.isArray(value.tools) ? value.tools.map(toLintableTool) : undefined;
  const subAgents = Array.isArray(value.subAgents)
    ? value.subAgents.filter((s): s is string => typeof s === 'string')
    : undefined;
  return {
    name: typeof value.name === 'string' ? value.name : undefined,
    instructions: typeof value.instructions === 'string' ? value.instructions : undefined,
    tools,
    subAgents,
    model: typeof value.model === 'string' ? value.model : undefined,
  };
}

function toLintableTool(value: unknown): LintableTool {
  if (!isPlainObject(value)) return {};
  return {
    name: typeof value.name === 'string' ? value.name : undefined,
    description: typeof value.description === 'string' ? value.description : undefined,
  };
}

/** Default iterator: yields exactly one LintableAgent (no tree walk). */
function* defaultAgentIterator(root: unknown): Iterable<LintableAgent> {
  const agent = unwrapAgent(root);
  if (agent) yield agent;
}

// ---------------------------------------------------------------------------
// Rule table — each rule is a pure function that emits zero or more findings
// for a given LintableAgent (plus a top-level pass for L004 + model kwarg).
// ---------------------------------------------------------------------------

const ENV_VAR_RE = /\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?/g;

const DATABRICKS_MODEL_PREFIXES = ['databricks-', 'endpoints/'] as const;

function isKnownDatabricksModel(name: string): boolean {
  return DATABRICKS_MODEL_PREFIXES.some((p) => name.startsWith(p));
}

interface PerAgentRule {
  readonly rule: string;
  readonly check: (agent: LintableAgent) => Iterable<LintFinding>;
}

const PER_AGENT_RULES: ReadonlyArray<PerAgentRule> = [
  // L001 — missing instructions
  {
    rule: 'L001',
    check: function* (agent) {
      const instructions = agent.instructions ?? '';
      if (instructions.trim() === '') {
        yield {
          rule: 'L001',
          severity: Severity.Error,
          message:
            'LlmAgent has no instructions set. The LLM will receive no system ' +
            'prompt and its behavior is undefined. Pass instructions in the ' +
            'agent config.',
          ...(agent.name ? { agentName: agent.name } : {}),
        };
      }
    },
  },
  // L002 — tools missing descriptions
  {
    rule: 'L002',
    check: function* (agent) {
      for (const tool of agent.tools ?? []) {
        const desc = (tool.description ?? '').trim();
        if (desc === '') {
          const toolName = tool.name ?? '<anonymous>';
          yield {
            rule: 'L002',
            severity: Severity.Warning,
            message:
              `Tool '${toolName}' has no description. The LLM uses the tool ` +
              'description to decide when to call it; without one the LLM ' +
              'cannot reliably pick this tool.',
            toolName,
            ...(agent.name ? { agentName: agent.name } : {}),
          };
        }
      }
    },
  },
  // L003 — sub-agent URLs with unresolved $ENV_VAR refs
  {
    rule: 'L003',
    check: function* (agent) {
      for (const url of agent.subAgents ?? []) {
        // Re-anchor the regex for each url (stateful /g flag).
        const re = new RegExp(ENV_VAR_RE.source, 'g');
        let match: RegExpExecArray | null;
        while ((match = re.exec(url)) !== null) {
          const varName = match[1]!;
          const envValue = process.env[varName];
          if (envValue === undefined || envValue === '') {
            yield {
              rule: 'L003',
              severity: Severity.Error,
              message:
                `Sub-agent URL '${url}' references $${varName} but the env ` +
                'var is not set. The agent will fail at compile time with an ' +
                'unresolved variable error.',
              ...(agent.name ? { agentName: agent.name } : {}),
            };
          }
        }
      }
    },
  },
];

function* checkModelName(
  agents: ReadonlyArray<LintableAgent>,
  explicitModel: string | undefined,
): Iterable<LintFinding> {
  // Mirrors Python's de-duplication: (location, model) pairs, keyed on model.
  const candidates: Array<{ agentName?: string; value: string }> = [];
  if (explicitModel) candidates.push({ value: explicitModel });
  for (const agent of agents) {
    if (agent.model) candidates.push({ agentName: agent.name, value: agent.model });
  }
  const seen = new Set<string>();
  for (const { agentName, value } of candidates) {
    if (seen.has(value)) continue;
    seen.add(value);
    if (!isKnownDatabricksModel(value)) {
      yield {
        rule: 'L004',
        severity: Severity.Warning,
        message:
          `Model name '${value}' doesn't start with a known Databricks ` +
          `endpoint prefix (${DATABRICKS_MODEL_PREFIXES.join(', ')}). This ` +
          'may be a typo or a custom endpoint — verify before deploy.',
        ...(agentName ? { agentName } : {}),
      };
    }
  }
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

function findingSortKey(f: LintFinding): string {
  return `${f.rule} ${f.agentName ?? ''} ${f.toolName ?? ''} ${f.message}`;
}

/**
 * Run all static checks against an agent. Returns findings sorted by
 * `(rule, agentName, toolName, message)` for diff-friendly CI output.
 *
 * @param agent An agent value — see the module header for supported shapes.
 * @param opts.model Model endpoint name to lint (otherwise only `.model`
 *   already on the agent is checked).
 * @param opts.agentIterator Override the agent-walking strategy. Yields
 *   each agent to check, exactly once. Default unwraps `createAgentPlugin`
 *   output / `AgentExports` / plain `AgentConfig`.
 */
export function lintAgent(agent: unknown, opts: LintAgentOptions = {}): LintFinding[] {
  const parsed = LintAgentOptionsSchema.parse(opts);
  const iterator = parsed.agentIterator ?? defaultAgentIterator;

  const findings: LintFinding[] = [];
  const agents: LintableAgent[] = [];
  for (const item of iterator(agent)) {
    const normalised = unwrapAgent(item) ?? toLintableAgent(item);
    if (!normalised) continue;
    agents.push(normalised);
    for (const rule of PER_AGENT_RULES) {
      for (const finding of rule.check(normalised)) {
        findings.push(finding);
      }
    }
  }

  for (const finding of checkModelName(agents, parsed.model)) {
    findings.push(finding);
  }

  findings.sort((a, b) => findingSortKey(a).localeCompare(findingSortKey(b)));
  return findings;
}
