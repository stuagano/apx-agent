#!/usr/bin/env node
/**
 * apx — command-line interface for the apx-internal-runtime (TypeScript starter).
 *
 * This is the TypeScript port of `python/src/apx_agent/cli.py`. The Python
 * CLI exposes ~21 commands; this starter ships the five that prove out the
 * core CLI architecture: `info`, `lint`, `list`, `test`, `version`. Future
 * waves extend the same shape — one file per command in `./commands/`, each
 * registered onto a Commander program built by {@link buildProgram}.
 *
 * # Running it
 *
 *   - After `npm run build` the bundled entry lives at `dist/cli/index.mjs`
 *     and the `bin` field in `package.json` exposes it as the `apx`
 *     executable to anyone who `npm install`s `apx-internal-runtime`.
 *   - From a checkout: `npx tsx src/cli/index.ts <command>`. Note that
 *     loading TS agent modules via dynamic `import()` still requires `tsx`
 *     at the user end too — see `./load-agent.ts` for details.
 *
 * # Test-friendly factory
 *
 * `buildProgram()` returns a `Command` with `.exitOverride()` enabled by
 * default. Tests call `program.parseAsync(['node', 'apx', 'version'])`
 * directly and assert against spied console output without ever
 * triggering `process.exit`. Production wires `process.exit` back into the
 * lint/test commands' exit hook so the process status reflects findings.
 */

import { Command } from 'commander';

import {
  registerConsolidateCommand,
  type ConsolidateCommandDeps,
} from './commands/consolidate.js';
import {
  registerDeployCommand,
  type DeployCommandDeps,
} from './commands/deploy.js';
import {
  registerExamplesCommand,
  type ExamplesCommandDeps,
} from './commands/examples.js';
import { registerInfoCommand, type InfoCommandDeps } from './commands/info.js';
import { registerLintCommand, type LintCommandDeps } from './commands/lint.js';
import { registerListCommand, type ListCommandDeps } from './commands/list.js';
import {
  registerMemoryCommand,
  type MemoryCommandDeps,
} from './commands/memory.js';
import {
  registerMineCommand,
  type MineCommandDeps,
} from './commands/mine.js';
import {
  registerScaffoldCommand,
  type ScaffoldCommandDeps,
} from './commands/scaffold.js';
import { registerTestCommand, type TestCommandDeps } from './commands/test.js';
import { registerVersionCommand } from './commands/version.js';

export interface BuildProgramDeps {
  info?: InfoCommandDeps;
  lint?: LintCommandDeps;
  list?: ListCommandDeps;
  test?: TestCommandDeps;
  memory?: MemoryCommandDeps;
  examples?: ExamplesCommandDeps;
  mine?: MineCommandDeps;
  consolidate?: ConsolidateCommandDeps;
  scaffold?: ScaffoldCommandDeps;
  deploy?: DeployCommandDeps;
}

/**
 * Build the Commander program. Tests inject `deps` to stub side effects;
 * the real CLI passes nothing.
 */
export function buildProgram(deps: BuildProgramDeps = {}): Command {
  const program = new Command();
  program
    .name('apx')
    .description('Declarative agents on Databricks (TypeScript starter).')
    .exitOverride();

  registerVersionCommand(program);
  registerInfoCommand(program, deps.info);
  registerLintCommand(program, deps.lint);
  registerListCommand(program, deps.list);
  registerTestCommand(program, deps.test);
  registerMemoryCommand(program, deps.memory);
  registerExamplesCommand(program, deps.examples);
  // Attach mine / consolidate AFTER the parent groups so they extend the
  // existing 'examples' / 'memory' commands rather than create duplicates.
  // (The registrars are idempotent — they look up the group, falling back
  // to create — but explicit ordering keeps the intent obvious.)
  registerMineCommand(program, deps.mine);
  registerConsolidateCommand(program, deps.consolidate);
  registerScaffoldCommand(program, deps.scaffold);
  registerDeployCommand(program, deps.deploy);

  return program;
}

async function main(): Promise<void> {
  const program = buildProgram();
  try {
    await program.parseAsync(process.argv);
  } catch (err) {
    // Commander throws `CommanderError` on `.exitOverride()` — re-emit the
    // exit code so the shell sees it. Other errors are unexpected; surface
    // them and exit non-zero.
    const code =
      typeof (err as { exitCode?: unknown }).exitCode === 'number'
        ? (err as { exitCode: number }).exitCode
        : 1;
    if (!(err instanceof Error) || !err.message.startsWith('(outputHelp)')) {
      const message = err instanceof Error ? err.message : String(err);
      if (message && !message.includes('outputHelp')) {
        // eslint-disable-next-line no-console
        console.error(`apx: ${message}`);
      }
    }
    process.exit(code);
  }
}

// Only run when invoked as a script (not when imported by the test suite).
// Compare `import.meta.url` to the file:// form of `process.argv[1]`. The
// argv path must be symlink-resolved first: when the CLI is installed or
// `npm link`-ed, argv[1] is the bin symlink (e.g. /usr/local/bin/apx) while
// import.meta.url already points at the real dist file, so an unresolved
// compare would be false and `main()` would silently never run.
const entryArg = process.argv[1];
let isDirect = false;
if (entryArg) {
  try {
    const { pathToFileURL } = await import('node:url');
    const { realpathSync } = await import('node:fs');
    let resolvedArg = entryArg;
    try {
      resolvedArg = realpathSync(entryArg);
    } catch {
      // fall back to the raw arg if it can't be resolved
    }
    isDirect = import.meta.url === pathToFileURL(resolvedArg).href;
  } catch {
    isDirect = false;
  }
}

if (isDirect) {
  void main();
}
