/**
 * apx lint — run static checks against an agent.
 *
 * Bridges to {@link lintAgent} (see `src/lint.ts`) and converts findings to
 * either a human-readable text report or a JSON array. Exits 1 if any
 * error-severity findings are reported; 0 otherwise. Warning findings are
 * surfaced but don't fail the command, matching the Python CLI.
 */

import type { Command } from 'commander';
import { z } from 'zod';

import { loadAgent } from '../load-agent.js';
import { lintAgent, Severity, type LintFinding } from '../../lint.js';
import { readApxAgentConfig } from '../pyproject.js';

const LintOptionsSchema = z.object({
  module: z.string().min(1),
  model: z.string().optional(),
  format: z.enum(['text', 'json']).default('text'),
});

function severityMarker(s: Severity): string {
  if (s === Severity.Error) return 'ERROR';
  if (s === Severity.Warning) return 'warn ';
  return 'info ';
}

function renderText(findings: LintFinding[]): string {
  if (findings.length === 0) return 'apx lint: clean — no findings.';
  const lines: string[] = [];
  for (const f of findings) {
    const loc = [f.agentName, f.toolName].filter(Boolean).join('/') || '-';
    lines.push(`  [${severityMarker(f.severity)}] ${f.rule}  ${loc}`);
    lines.push(`           ${f.message}`);
  }
  return lines.join('\n');
}

export interface LintCommandDeps {
  /** Override for tests — defaults to the real {@link loadAgent}. */
  loadAgent?: (spec: string) => Promise<unknown>;
  /**
   * Override the linter — tests inject a stub. Defaults to {@link lintAgent}.
   */
  lintAgent?: (agent: unknown, opts: { model?: string }) => LintFinding[];
  /** Override config reader. Defaults to {@link readApxAgentConfig}. */
  readConfig?: () => Record<string, unknown>;
  /**
   * Exit hook — tests pass a recorder; production passes `process.exit`.
   * Defaults to `process.exit`.
   */
  exit?: (code: number) => void;
}

export function registerLintCommand(program: Command, deps: LintCommandDeps = {}): void {
  const load = deps.loadAgent ?? loadAgent;
  const lint = deps.lintAgent ?? ((a, o) => lintAgent(a, o));
  const readConfig = deps.readConfig ?? readApxAgentConfig;
  const exit = deps.exit ?? ((c: number) => process.exit(c));

  program
    .command('lint')
    .description('Static checks for an agent — instructions, tool docs, sub-agent URLs, model name.')
    .requiredOption('--module <spec>', 'Agent module spec, "module:variable".', 'agent:agent')
    .option('--model <name>', 'Model endpoint to lint (in addition to any compiled into the tree).')
    .option('--format <fmt>', 'Output format (text|json).', 'text')
    .action(async (rawOpts: { module: string; model?: string; format: string }) => {
      const opts = LintOptionsSchema.parse(rawOpts);
      const agent = await load(opts.module);
      const cfg = readConfig();
      const effectiveModel =
        opts.model ?? (typeof cfg.model === 'string' ? cfg.model : undefined);
      const findings = lint(agent, { model: effectiveModel });

      if (opts.format === 'json') {
        // eslint-disable-next-line no-console
        console.log(
          JSON.stringify(
            findings.map((f) => ({
              code: f.rule,
              severity: f.severity,
              location: [f.agentName, f.toolName].filter(Boolean).join('/') || null,
              message: f.message,
            })),
            null,
            2,
          ),
        );
      } else {
        // eslint-disable-next-line no-console
        console.log(renderText(findings));
      }

      const errorCount = findings.filter((f) => f.severity === Severity.Error).length;
      if (errorCount > 0) {
        // eslint-disable-next-line no-console
        console.error(`\napx lint: ${errorCount} error(s).`);
        exit(1);
      }
    });
}
