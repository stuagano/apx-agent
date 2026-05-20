/**
 * apx deploy — log + deploy an agent (TS port of `python/src/apx_agent/cli.py`
 * deploy command's `--target apps` branch).
 *
 * This module focuses on the `--target apps` flow:
 *
 *   1. Pre-flight (databricks.yml + package.json + agent_server/ exist).
 *   2. `databricks bundle validate --target <target>`
 *   3. `databricks bundle deploy --target <target>`
 *   4. `databricks bundle run <app> --target <target>` (skippable)
 *   5. Poll `databricks apps get` until ACTIVE/RUNNING.
 *
 * The model-serving target is out of scope for this port and prints a
 * "not implemented" message — that path is owned by `chat-agent.ts`'s
 * `logAgent` and a future TS-side equivalent of `databricks.agents.deploy`.
 *
 * Tests inject the `runDatabricksCmd` + `pollAppReady` + `sleep` deps so
 * the entire subprocess + timing surface is mockable.
 */

import { existsSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { spawn } from 'node:child_process';

import type { Command } from 'commander';
import { z } from 'zod';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface CmdResult {
  exitCode: number;
  stdout: string;
  stderr: string;
}

export type RunDatabricksCmdFn = (
  args: string[],
  profile?: string,
) => Promise<CmdResult>;

export type SleepFn = (ms: number) => Promise<void>;

export type NowFn = () => number;

export interface DeployCommandDeps {
  runDatabricksCmd?: RunDatabricksCmdFn;
  sleep?: SleepFn;
  now?: NowFn;
  /** Defaults to existsSync. */
  exists?: (path: string) => boolean;
  /** Defaults to readFileSync(path, "utf8"). */
  readFile?: (path: string) => string;
  /** Defaults to process.cwd(). */
  cwd?: () => string;
  /** Defaults to console.error (stderr logger). */
  log?: (msg: string) => void;
  /** Defaults to console.log (stdout — the final summary). */
  out?: (msg: string) => void;
}

// ---------------------------------------------------------------------------
// Default impls
// ---------------------------------------------------------------------------

function defaultRunDatabricksCmd(args: string[], profile?: string): Promise<CmdResult> {
  const cmd = profile ? [...args, '--profile', profile] : args;
  return new Promise<CmdResult>((resolveP) => {
    const child = spawn('databricks', cmd, { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => (stdout += String(d)));
    child.stderr.on('data', (d) => (stderr += String(d)));
    child.on('error', (err) => {
      resolveP({ exitCode: 127, stdout, stderr: stderr + String(err) });
    });
    child.on('close', (code) => {
      resolveP({ exitCode: code ?? 1, stdout, stderr });
    });
  });
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function defaultDeps(): Required<DeployCommandDeps> {
  return {
    runDatabricksCmd: defaultRunDatabricksCmd,
    sleep: defaultSleep,
    now: () => Date.now(),
    exists: (path) => existsSync(path),
    readFile: (path) => readFileSync(path, 'utf8'),
    cwd: () => process.cwd(),
    log: (msg) => {
      // eslint-disable-next-line no-console
      console.error(msg);
    },
    out: (msg) => {
      // eslint-disable-next-line no-console
      console.log(msg);
    },
  };
}

// ---------------------------------------------------------------------------
// Bundle YAML parsing — minimal, hand-rolled, to avoid a yaml dep.
// We only need to read `resources.apps.<key>.name`. The scaffold guarantees a
// flat-enough shape that a regex parse is reliable in practice.
// ---------------------------------------------------------------------------

/**
 * Extract the bundle-key and workspace app name from a databricks.yml string.
 * Returns null if no `resources.apps.<key>:` block is found. Tolerates
 * arbitrary content before/after. The naive grep is sufficient because the
 * scaffold writes a known shape; complex parses can wire a real YAML parser
 * later.
 */
export function parseBundleAppName(yml: string): { bundleKey: string; appName: string } | null {
  // Find the resources.apps block by indentation.
  const lines = yml.split(/\r?\n/);
  let inApps = false;
  let appsIndent = -1;
  let bundleKey: string | null = null;
  let bundleKeyIndent = -1;
  let appName: string | null = null;

  for (const line of lines) {
    const trimmed = line.replace(/\t/g, '  ');
    const stripped = trimmed.trimStart();
    const indent = trimmed.length - stripped.length;

    if (!inApps) {
      // Look for "apps:" nested under "resources:".
      if (stripped.startsWith('apps:')) {
        inApps = true;
        appsIndent = indent;
      }
      continue;
    }

    // We're inside resources.apps now.
    if (indent <= appsIndent && stripped.length > 0 && !stripped.startsWith('#')) {
      // Left the apps block.
      break;
    }
    if (bundleKey === null) {
      // First child key after `apps:` — `<bundle_key>:`
      const m = stripped.match(/^([A-Za-z0-9_.-]+)\s*:\s*$/);
      if (m) {
        bundleKey = m[1];
        bundleKeyIndent = indent;
      }
      continue;
    }
    // Within the bundle-key block — look for `name: ...`
    if (indent <= bundleKeyIndent) {
      // Either a sibling app or back out of apps; we only support a single
      // app, so stop reading.
      break;
    }
    const nm = stripped.match(/^name\s*:\s*"?([^"\s]+)"?\s*$/);
    if (nm) {
      appName = nm[1];
      break;
    }
  }

  if (!bundleKey) return null;
  return { bundleKey, appName: appName ?? bundleKey };
}

// ---------------------------------------------------------------------------
// Pre-flight
// ---------------------------------------------------------------------------

export interface PreflightOptions {
  cwd: string;
  exists: (path: string) => boolean;
}

/** Verify cwd looks like a scaffolded apps project. Throws on first miss. */
export function preflightApps(opts: PreflightOptions): void {
  const missing: string[] = [];
  if (!opts.exists(join(opts.cwd, 'databricks.yml'))) missing.push('databricks.yml');
  if (!opts.exists(join(opts.cwd, 'package.json'))) missing.push('package.json');
  if (!opts.exists(join(opts.cwd, 'agent_server'))) missing.push('agent_server/');
  if (missing.length > 0) {
    throw new Error(
      `Pre-flight failed for --target apps. Missing in current directory: ${missing.join(', ')}. ` +
        'Run `apx scaffold <name> --target apps` to generate the expected layout.',
    );
  }
}

// ---------------------------------------------------------------------------
// Poll-for-ready
// ---------------------------------------------------------------------------

export interface PollAppReadyOptions {
  appName: string;
  profile?: string;
  timeoutMs?: number;
  runDatabricksCmd: RunDatabricksCmdFn;
  sleep: SleepFn;
  now: NowFn;
  log: (msg: string) => void;
}

/**
 * Poll `databricks apps get <appName>` until ACTIVE/RUNNING or terminal
 * failure or timeout. Returns the final JSON payload. Mirrors the Python
 * `_poll_app_ready` state machine with exponential backoff capped at 15s.
 */
export async function pollAppReady(opts: PollAppReadyOptions): Promise<Record<string, unknown>> {
  const timeoutMs = opts.timeoutMs ?? 300_000;
  const deadline = opts.now() + timeoutMs;
  let delay = 1000;
  let lastState: [string, string] = ['', ''];

  while (true) {
    const proc = await opts.runDatabricksCmd(
      ['apps', 'get', opts.appName, '-o', 'json'],
      opts.profile,
    );
    if (proc.exitCode !== 0) {
      opts.log(`  (apps get returned ${proc.exitCode}; retrying)`);
    } else {
      let payload: Record<string, unknown> = {};
      try {
        payload = JSON.parse(proc.stdout) as Record<string, unknown>;
      } catch {
        payload = {};
      }
      const computeState = String(
        ((payload['compute_status'] as Record<string, unknown> | undefined)?.['state'] ?? '') as string,
      ).toUpperCase();
      const appState = String(
        ((payload['app_status'] as Record<string, unknown> | undefined)?.['state'] ?? '') as string,
      ).toUpperCase();
      if (computeState !== lastState[0] || appState !== lastState[1]) {
        opts.log(`  status: compute=${computeState || '?'} app=${appState || '?'}`);
        lastState = [computeState, appState];
      }
      if (computeState === 'ACTIVE' && appState === 'RUNNING') {
        return payload;
      }
      if (computeState === 'ERROR' || appState === 'CRASHED') {
        throw new Error(
          `App ${opts.appName} entered terminal failure (compute=${computeState}, app=${appState}). ` +
            'Inspect logs via `databricks apps logs`.',
        );
      }
    }

    if (opts.now() >= deadline) {
      throw new Error(
        `Timed out after ${Math.round(timeoutMs / 1000)}s waiting for app ${opts.appName} ` +
          `to reach ACTIVE/RUNNING. Last observed: compute=${lastState[0] || '?'} app=${lastState[1] || '?'}.`,
      );
    }
    await opts.sleep(Math.min(delay, Math.max(0, deadline - opts.now())));
    delay = Math.min(delay * 1.5, 15_000);
  }
}

// ---------------------------------------------------------------------------
// Main deploy-apps impl
// ---------------------------------------------------------------------------

export interface DeployAppsOptions {
  profile?: string;
  bundleTarget: string;
  noRun: boolean;
  jsonOutput: boolean;
}

export async function deployAppsImpl(
  opts: DeployAppsOptions,
  deps: Required<DeployCommandDeps>,
): Promise<void> {
  const cwd = deps.cwd();
  deps.log(
    `# apx deploy --target apps (bundle-target=${opts.bundleTarget}, profile=${opts.profile ?? '<default>'})`,
  );

  preflightApps({ cwd, exists: deps.exists });

  const yml = deps.readFile(join(cwd, 'databricks.yml'));
  const parsed = parseBundleAppName(yml);
  if (!parsed) {
    throw new Error(
      'databricks.yml has no resources.apps.<name> entry. --target apps requires exactly one app declared.',
    );
  }
  const { bundleKey, appName } = parsed;
  if (bundleKey !== appName) {
    deps.log(`# resolved bundle_key=${bundleKey} app_name=${appName}`);
  } else {
    deps.log(`# resolved app_name: ${appName}`);
  }

  // 1. validate
  deps.log('# databricks bundle validate');
  const validate = await deps.runDatabricksCmd(
    ['bundle', 'validate', '--target', opts.bundleTarget],
    opts.profile,
  );
  if (validate.exitCode !== 0) {
    const msg = `databricks bundle validate failed (exit ${validate.exitCode}): ${tail(
      validate.stderr || validate.stdout,
    )}`;
    if (opts.jsonOutput) {
      deps.out(JSON.stringify({ ok: false, error: msg, app_name: appName }));
      throw new ExitError(1);
    }
    throw new Error(msg);
  }

  // 2. deploy
  deps.log('# databricks bundle deploy');
  const deployStart = deps.now();
  const deployed = await deps.runDatabricksCmd(
    ['bundle', 'deploy', '--target', opts.bundleTarget],
    opts.profile,
  );
  const deploySeconds = Math.round((deps.now() - deployStart) / 100) / 10;
  if (deployed.exitCode !== 0) {
    const msg = `databricks bundle deploy failed (exit ${deployed.exitCode}): ${tail(
      deployed.stderr || deployed.stdout,
    )}`;
    if (opts.jsonOutput) {
      deps.out(JSON.stringify({ ok: false, error: msg, app_name: appName }));
      throw new ExitError(1);
    }
    throw new Error(msg);
  }
  deps.log(`  bundle deploy finished in ${deploySeconds}s`);

  // 3. run
  let runSeconds: number | null = null;
  if (!opts.noRun) {
    deps.log(`# databricks bundle run ${bundleKey}`);
    const runStart = deps.now();
    const ran = await deps.runDatabricksCmd(
      ['bundle', 'run', bundleKey, '--target', opts.bundleTarget],
      opts.profile,
    );
    runSeconds = Math.round((deps.now() - runStart) / 100) / 10;
    if (ran.exitCode !== 0) {
      deps.log(`  bundle run returned ${ran.exitCode} (continuing)`);
      deps.log(`  last lines: ${tail(ran.stderr || ran.stdout)}`);
    } else {
      deps.log(`  bundle run finished in ${runSeconds}s`);
    }
  } else {
    deps.log('# --no-run: skipping bundle run');
  }

  // 4. poll
  deps.log(`# polling databricks apps get ${appName} for ACTIVE/RUNNING`);
  const payload = await pollAppReady({
    appName,
    profile: opts.profile,
    runDatabricksCmd: deps.runDatabricksCmd,
    sleep: deps.sleep,
    now: deps.now,
    log: deps.log,
  });
  const appUrl = typeof payload['url'] === 'string' ? (payload['url'] as string) : '';

  // 5. report
  if (opts.jsonOutput) {
    deps.out(
      JSON.stringify({
        ok: true,
        app_name: appName,
        app_url: appUrl,
        bundle_target: opts.bundleTarget,
        deploy_seconds: deploySeconds,
        run_seconds: runSeconds,
      }),
    );
  } else {
    deps.log(`# app ready: ${appName}`);
    deps.out(appUrl);
  }
}

function tail(text: string, n = 20): string {
  if (!text) return '';
  return text.split(/\r?\n/).slice(-n).join('\n');
}

/** A sentinel error commander can swallow with a clean exit code. */
export class ExitError extends Error {
  constructor(public readonly code: number) {
    super(`exit ${code}`);
  }
}

// ---------------------------------------------------------------------------
// Commander registration
// ---------------------------------------------------------------------------

const DeployOptionsSchema = z.object({
  target: z.enum(['model-serving', 'apps']).default('model-serving'),
  profile: z.string().min(1).optional(),
  bundleTarget: z.string().min(1).default('dev'),
  noRun: z.boolean().default(false),
  jsonOutput: z.boolean().default(false),
});

export function registerDeployCommand(
  program: Command,
  depsOverride: DeployCommandDeps = {},
): void {
  // Snapshot non-injectable defaults at registration; the inner action
  // re-merges injectables on every call so test specs can swap things
  // between cases.
  program
    .command('deploy')
    .description('Deploy the agent (Model Serving or Apps).')
    .option('--target <target>', "Deployment target ('model-serving' | 'apps').", 'model-serving')
    .option('--profile <profile>', 'Databricks CLI profile.')
    .option('--bundle-target <target>', 'DAB target name (apps only).', 'dev')
    // commander: --no-X defaults `X` to true; passing --no-X sets it false.
    .option('--no-run', 'Skip `databricks bundle run` (apps only).')
    .option('--json-output', 'Emit single JSON summary on stdout.', false)
    .action(
      async (rawOpts: {
        target?: string;
        profile?: string;
        bundleTarget?: string;
        run?: boolean;
        jsonOutput?: boolean;
      }) => {
        const opts = DeployOptionsSchema.parse({
          target: rawOpts.target,
          profile: rawOpts.profile,
          bundleTarget: rawOpts.bundleTarget,
          // Commander negates --no-run into `run = false`. Coerce.
          noRun: rawOpts.run === false,
          jsonOutput: Boolean(rawOpts.jsonOutput),
        });
        if (opts.target !== 'apps') {
          throw new Error(
            "apx deploy: only '--target apps' is implemented in the TS port. " +
              'For model-serving, use the logAgent() programmatic API.',
          );
        }
        const deps = { ...defaultDeps(), ...depsOverride };
        try {
          await deployAppsImpl(
            {
              profile: opts.profile,
              bundleTarget: opts.bundleTarget,
              noRun: opts.noRun,
              jsonOutput: opts.jsonOutput,
            },
            deps,
          );
        } catch (err) {
          if (err instanceof ExitError) {
            // Don't re-raise — output already emitted.
            throw err;
          }
          throw err;
        }
      },
    );
}

// statSync re-export so we don't need to import in tests separately.
void statSync;
