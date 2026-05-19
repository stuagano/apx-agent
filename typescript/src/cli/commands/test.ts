/**
 * apx test — local smoke test: compile the agent and send one prompt through.
 *
 * Bridges to {@link runOnce} from `../../run-once.js`. The Python CLI uses
 * `compile_to_chat_agent` + `predict`; the TS port has no MLflow ChatAgent
 * compiler — `runOnce` plays the same role (single-prompt invoke against the
 * configured model, returning the final assistant text).
 *
 * Exits non-zero when the underlying call rejects, so this command is safe
 * to drop into CI as a pre-deploy gate alongside `apx lint`.
 */

import type { Command } from 'commander';
import { z } from 'zod';

import { loadAgent } from '../load-agent.js';
import { runOnce as defaultRunOnce, type RunOnceOptions } from '../../run-once.js';
import { readApxAgentConfig } from '../pyproject.js';

const TestOptionsSchema = z.object({
  module: z.string().min(1),
  prompt: z.string().min(1).default('hi'),
  model: z.string().optional(),
});

export interface TestCommandDeps {
  /** Override for tests — defaults to the real {@link loadAgent}. */
  loadAgent?: (spec: string) => Promise<unknown>;
  /** Override for tests — defaults to the real {@link runOnce}. */
  runOnce?: (opts: RunOnceOptions) => Promise<string>;
  /** Override config reader. Defaults to {@link readApxAgentConfig}. */
  readConfig?: () => Record<string, unknown>;
  /** Exit hook — defaults to `process.exit`. */
  exit?: (code: number) => void;
}

export function registerTestCommand(program: Command, deps: TestCommandDeps = {}): void {
  const load = deps.loadAgent ?? loadAgent;
  const runner = deps.runOnce ?? defaultRunOnce;
  const readConfig = deps.readConfig ?? readApxAgentConfig;
  const exit = deps.exit ?? ((c: number) => process.exit(c));

  program
    .command('test')
    .description('Compile the agent locally and run a single prompt through it.')
    .requiredOption('--module <spec>', 'Agent module spec, "module:variable".', 'agent:agent')
    .option('--prompt <text>', 'Prompt to send.', 'hi')
    .option('--model <name>', 'LLM endpoint. Defaults to [tool.apx.agent].model in pyproject.toml.')
    .action(async (rawOpts: { module: string; prompt: string; model?: string }) => {
      const opts = TestOptionsSchema.parse(rawOpts);
      const agent = await load(opts.module);
      const cfg = readConfig();
      const effectiveModel =
        opts.model ?? (typeof cfg.model === 'string' ? cfg.model : undefined);

      const start = Date.now();
      try {
        const result = await runner({
          agent,
          prompt: opts.prompt,
          ...(effectiveModel ? { model: effectiveModel } : {}),
        });
        const elapsed = Date.now() - start;
        const preview = (result ?? '').split(/\r?\n/)[0] ?? '';
        // eslint-disable-next-line no-console
        console.log(`--- prompt: ${JSON.stringify(opts.prompt)}`);
        // eslint-disable-next-line no-console
        console.log(`    ok  (${elapsed} ms)  ${preview.slice(0, 120)}`);
      } catch (err) {
        const elapsed = Date.now() - start;
        const name = err instanceof Error ? err.constructor.name : 'Error';
        const message = err instanceof Error ? err.message : String(err);
        // eslint-disable-next-line no-console
        console.error(`    FAIL (${elapsed} ms): ${name}: ${message}`);
        exit(1);
      }
    });
}
