/**
 * Tests for `apx deploy --target apps`.
 *
 * Heavily injectable — runDatabricksCmd is stubbed to record arg vectors and
 * return canned CmdResults, sleep is replaced with an immediate Promise, and
 * now() is faked so the poll loop's exponential backoff doesn't gate the
 * test on wall time.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { buildProgram } from '../src/cli/index.js';
import type {
  DeployCommandDeps,
  CmdResult,
  RunDatabricksCmdFn,
} from '../src/cli/commands/deploy.js';
import { parseBundleAppName, pollAppReady } from '../src/cli/commands/deploy.js';

let logSpy: ReturnType<typeof vi.spyOn>;
let errSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
  errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  logSpy.mockRestore();
  errSpy.mockRestore();
  vi.restoreAllMocks();
});

function stdout(): string {
  return logSpy.mock.calls.map((c) => c.join(' ')).join('\n');
}

const SAMPLE_YML = `bundle:
  name: my_app
resources:
  apps:
    my_app:
      name: my_app
      description: test
`;

function fakeRun(cases: Record<string, CmdResult>): {
  fn: RunDatabricksCmdFn;
  calls: string[][];
} {
  const calls: string[][] = [];
  const fn: RunDatabricksCmdFn = async (args) => {
    calls.push(args);
    const key = args[0]; // 'bundle' or 'apps'
    const sub = args[1]; // 'validate'/'deploy'/'run'/'get'
    const candidate = `${key} ${sub}`;
    return cases[candidate] ?? cases[args.join(' ')] ?? { exitCode: 0, stdout: '', stderr: '' };
  };
  return { fn, calls };
}

function makeDeps(overrides: Partial<DeployCommandDeps>): DeployCommandDeps {
  let t = 1_700_000_000_000;
  return {
    sleep: async () => {},
    now: () => {
      t += 100;
      return t;
    },
    exists: () => true,
    readFile: () => SAMPLE_YML,
    cwd: () => '/fake/cwd',
    log: () => {},
    out: (msg) => {
      // forward to console.log so test can sniff
      // eslint-disable-next-line no-console
      console.log(msg);
    },
    ...overrides,
  };
}

async function runCli(args: string[], deps: Parameters<typeof buildProgram>[0] = {}) {
  const program = buildProgram(deps);
  await program.parseAsync(['node', 'apx', ...args]);
}

// ---------------------------------------------------------------------------
// parseBundleAppName — unit
// ---------------------------------------------------------------------------

describe('parseBundleAppName', () => {
  it('parses bundleKey + appName from a well-formed YAML', () => {
    expect(parseBundleAppName(SAMPLE_YML)).toEqual({
      bundleKey: 'my_app',
      appName: 'my_app',
    });
  });

  it('returns null when no resources.apps block exists', () => {
    expect(parseBundleAppName('bundle:\n  name: x\n')).toBeNull();
  });

  it('falls back to bundleKey when name is omitted', () => {
    const yml = `bundle:\n  name: x\nresources:\n  apps:\n    keyOnly:\n      description: x\n`;
    expect(parseBundleAppName(yml)).toEqual({
      bundleKey: 'keyOnly',
      appName: 'keyOnly',
    });
  });
});

// ---------------------------------------------------------------------------
// pollAppReady — state-machine
// ---------------------------------------------------------------------------

describe('pollAppReady', () => {
  it('returns the payload when the app is ACTIVE/RUNNING', async () => {
    let t = 1000;
    const payload = await pollAppReady({
      appName: 'a',
      runDatabricksCmd: async () => ({
        exitCode: 0,
        stdout: JSON.stringify({
          compute_status: { state: 'ACTIVE' },
          app_status: { state: 'RUNNING' },
          url: 'https://example',
        }),
        stderr: '',
      }),
      sleep: async () => {},
      now: () => {
        t += 100;
        return t;
      },
      log: () => {},
    });
    expect(payload.url).toBe('https://example');
  });

  it('throws on terminal failure (CRASHED)', async () => {
    let t = 1000;
    await expect(
      pollAppReady({
        appName: 'a',
        runDatabricksCmd: async () => ({
          exitCode: 0,
          stdout: JSON.stringify({
            compute_status: { state: 'ACTIVE' },
            app_status: { state: 'CRASHED' },
          }),
          stderr: '',
        }),
        sleep: async () => {},
        now: () => {
          t += 100;
          return t;
        },
        log: () => {},
      }),
    ).rejects.toThrow(/terminal failure/);
  });
});

// ---------------------------------------------------------------------------
// deploy command — wiring
// ---------------------------------------------------------------------------

describe('apx deploy --target apps', () => {
  it('fails fast when databricks.yml is missing', async () => {
    const deps = makeDeps({
      exists: (path) => !path.endsWith('databricks.yml'),
    });
    await expect(
      runCli(['deploy', '--target', 'apps'], { deploy: deps }),
    ).rejects.toThrow(/Pre-flight failed/);
  });

  it('runs validate → deploy → run → poll in order', async () => {
    const { fn: runFn, calls } = fakeRun({
      'bundle validate': { exitCode: 0, stdout: '', stderr: '' },
      'bundle deploy': { exitCode: 0, stdout: '', stderr: '' },
      'bundle run': { exitCode: 0, stdout: '', stderr: '' },
      'apps get': {
        exitCode: 0,
        stdout: JSON.stringify({
          compute_status: { state: 'ACTIVE' },
          app_status: { state: 'RUNNING' },
          url: 'https://app.example',
        }),
        stderr: '',
      },
    });
    const deps = makeDeps({ runDatabricksCmd: runFn });
    await runCli(['deploy', '--target', 'apps'], { deploy: deps });
    const order = calls.map((c) => `${c[0]} ${c[1]}`);
    expect(order[0]).toBe('bundle validate');
    expect(order[1]).toBe('bundle deploy');
    expect(order[2]).toBe('bundle run');
    expect(order[3]).toBe('apps get');
    expect(stdout()).toContain('https://app.example');
  });

  it('skips bundle run when --no-run is passed', async () => {
    const { fn: runFn, calls } = fakeRun({
      'bundle validate': { exitCode: 0, stdout: '', stderr: '' },
      'bundle deploy': { exitCode: 0, stdout: '', stderr: '' },
      'apps get': {
        exitCode: 0,
        stdout: JSON.stringify({
          compute_status: { state: 'ACTIVE' },
          app_status: { state: 'RUNNING' },
          url: 'https://x',
        }),
        stderr: '',
      },
    });
    const deps = makeDeps({ runDatabricksCmd: runFn });
    await runCli(['deploy', '--target', 'apps', '--no-run'], { deploy: deps });
    const order = calls.map((c) => `${c[0]} ${c[1]}`);
    expect(order).not.toContain('bundle run');
  });

  it('--json-output emits a single JSON object on success', async () => {
    const { fn: runFn } = fakeRun({
      'bundle validate': { exitCode: 0, stdout: '', stderr: '' },
      'bundle deploy': { exitCode: 0, stdout: '', stderr: '' },
      'bundle run': { exitCode: 0, stdout: '', stderr: '' },
      'apps get': {
        exitCode: 0,
        stdout: JSON.stringify({
          compute_status: { state: 'ACTIVE' },
          app_status: { state: 'RUNNING' },
          url: 'https://j',
        }),
        stderr: '',
      },
    });
    const deps = makeDeps({ runDatabricksCmd: runFn });
    await runCli(['deploy', '--target', 'apps', '--json-output'], { deploy: deps });

    // The last stdout line must be parseable JSON with ok=true.
    const lines = stdout().split('\n').filter((l) => l.trim().length > 0);
    const last = lines[lines.length - 1];
    const parsed = JSON.parse(last);
    expect(parsed.ok).toBe(true);
    expect(parsed.app_name).toBe('my_app');
    expect(parsed.app_url).toBe('https://j');
  });

  it('surfaces validate failures with a clear error', async () => {
    const { fn: runFn } = fakeRun({
      'bundle validate': { exitCode: 2, stdout: '', stderr: 'broken yaml' },
    });
    const deps = makeDeps({ runDatabricksCmd: runFn });
    await expect(
      runCli(['deploy', '--target', 'apps'], { deploy: deps }),
    ).rejects.toThrow(/bundle validate failed/);
  });
});
