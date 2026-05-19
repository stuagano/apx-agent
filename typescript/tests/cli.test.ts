/**
 * Tests for the `apx` CLI starter.
 *
 * Each command is registered onto a Commander program by a factory in
 * `src/cli/index.ts`. The tests build that program with stubbed deps,
 * capture `console.log` / `console.error` via spies, and assert on the
 * captured output. `program.exitOverride()` is already on by default, so
 * commander throws `CommanderError` instead of calling `process.exit`.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';

import { buildProgram } from '../src/cli/index.js';
import { parseModuleSpec, resolveModuleSpecifier, loadAgent } from '../src/cli/load-agent.js';
import { readApxAgentConfig, parseApxAgentSection } from '../src/cli/pyproject.js';
import type { LintFinding } from '../src/lint.js';
import { Severity } from '../src/lint.js';
import type { Topology, RegisteredModel } from '../src/topology.js';

// ---------------------------------------------------------------------------
// console spy helpers
// ---------------------------------------------------------------------------

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

function stderr(): string {
  return errSpy.mock.calls.map((c) => c.join(' ')).join('\n');
}

async function runCli(
  args: string[],
  deps: Parameters<typeof buildProgram>[0] = {},
): Promise<void> {
  const program = buildProgram(deps);
  await program.parseAsync(['node', 'apx', ...args]);
}

// ---------------------------------------------------------------------------
// version
// ---------------------------------------------------------------------------

describe('apx version', () => {
  it('prints a non-empty version string', async () => {
    await runCli(['version']);
    expect(stdout().trim().length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// info
// ---------------------------------------------------------------------------

describe('apx info', () => {
  const fakeAgent = {
    instructions: 'You are a helpful assistant.',
    tools: [
      { name: 'echo', description: 'Echo a message back.' },
      { name: 'add', description: 'Add two numbers.' },
    ],
    subAgents: ['endpoints/other-agent'],
    model: 'databricks-claude-sonnet-4-6',
  };

  it('renders text format with tools and sub-agents', async () => {
    await runCli(['info', '--module', 'fake:agent'], {
      info: { loadAgent: async () => fakeAgent },
    });
    const out = stdout();
    expect(out).toContain('Agent loaded from fake:agent');
    expect(out).toContain('Tools (2)');
    expect(out).toContain('- echo');
    expect(out).toContain('- add');
    expect(out).toContain('Sub-agents (1)');
    expect(out).toContain('endpoints/other-agent');
  });

  it('renders JSON format that round-trips through JSON.parse', async () => {
    await runCli(['info', '--module', 'fake:agent', '--format', 'json'], {
      info: { loadAgent: async () => fakeAgent },
    });
    const parsed = JSON.parse(stdout());
    expect(parsed.module).toBe('fake:agent');
    expect(parsed.tools).toHaveLength(2);
    expect(parsed.tools[0].name).toBe('echo');
    expect(parsed.sub_agents).toEqual(['endpoints/other-agent']);
  });

  it('unwraps AgentExports-shaped agents via getConfig/getTools', async () => {
    const exportsShape = {
      getConfig: () => ({
        instructions: 'hi',
        tools: [],
        subAgents: [],
        model: 'databricks-foo',
      }),
      getTools: () => [{ name: 'wrapped', description: 'Wrapped tool.' }],
      getToolSchemas: () => [],
    };
    await runCli(['info', '--module', 'fake:agent'], {
      info: { loadAgent: async () => exportsShape },
    });
    expect(stdout()).toContain('- wrapped');
  });
});

// ---------------------------------------------------------------------------
// lint
// ---------------------------------------------------------------------------

describe('apx lint', () => {
  it('exits 0 on a clean agent', async () => {
    const exitSpy = vi.fn();
    await runCli(['lint', '--module', 'fake:agent'], {
      lint: {
        loadAgent: async () => ({}),
        lintAgent: () => [],
        readConfig: () => ({}),
        exit: exitSpy,
      },
    });
    expect(exitSpy).not.toHaveBeenCalled();
    expect(stdout()).toContain('clean');
  });

  it('exits 1 when an error finding is reported', async () => {
    const exitSpy = vi.fn();
    const findings: LintFinding[] = [
      {
        rule: 'L001',
        severity: Severity.Error,
        message: 'LlmAgent has no instructions set.',
      },
    ];
    await runCli(['lint', '--module', 'fake:agent'], {
      lint: {
        loadAgent: async () => ({}),
        lintAgent: () => findings,
        readConfig: () => ({}),
        exit: exitSpy,
      },
    });
    expect(exitSpy).toHaveBeenCalledWith(1);
    expect(stdout()).toContain('L001');
    expect(stderr()).toContain('1 error');
  });

  it('emits JSON in --format json mode', async () => {
    const findings: LintFinding[] = [
      {
        rule: 'L002',
        severity: Severity.Warning,
        message: 'Tool has no description.',
        toolName: 'do_thing',
        agentName: 'helper',
      },
    ];
    await runCli(['lint', '--module', 'fake:agent', '--format', 'json'], {
      lint: {
        loadAgent: async () => ({}),
        lintAgent: () => findings,
        readConfig: () => ({}),
        exit: vi.fn(),
      },
    });
    const parsed = JSON.parse(stdout());
    expect(parsed).toHaveLength(1);
    expect(parsed[0].code).toBe('L002');
    expect(parsed[0].severity).toBe('warning');
  });

  it('passes model from pyproject config when --model is omitted', async () => {
    const lintSpy = vi.fn(() => []);
    await runCli(['lint', '--module', 'fake:agent'], {
      lint: {
        loadAgent: async () => ({}),
        lintAgent: lintSpy,
        readConfig: () => ({ model: 'databricks-from-config' }),
        exit: vi.fn(),
      },
    });
    expect(lintSpy).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ model: 'databricks-from-config' }),
    );
  });
});

// ---------------------------------------------------------------------------
// test
// ---------------------------------------------------------------------------

describe('apx test', () => {
  it('calls runOnce and prints the result preview', async () => {
    const runOnceSpy = vi.fn(async () => 'hello from the agent');
    await runCli(['test', '--module', 'fake:agent', '--prompt', 'hi', '--model', 'm'], {
      test: {
        loadAgent: async () => ({}),
        runOnce: runOnceSpy,
        readConfig: () => ({}),
        exit: vi.fn(),
      },
    });
    expect(runOnceSpy).toHaveBeenCalledWith(
      expect.objectContaining({ prompt: 'hi', model: 'm' }),
    );
    expect(stdout()).toContain('hello from the agent');
    expect(stdout()).toMatch(/ok\s+\(\d+ ms\)/);
  });

  it('exits non-zero when runOnce rejects', async () => {
    const exitSpy = vi.fn();
    await runCli(['test', '--module', 'fake:agent', '--prompt', 'hi', '--model', 'm'], {
      test: {
        loadAgent: async () => ({}),
        runOnce: async () => {
          throw new Error('boom');
        },
        readConfig: () => ({}),
        exit: exitSpy,
      },
    });
    expect(exitSpy).toHaveBeenCalledWith(1);
    expect(stderr()).toContain('FAIL');
    expect(stderr()).toContain('boom');
  });

  it('falls back to pyproject [tool.apx.agent].model when --model is omitted', async () => {
    const runOnceSpy = vi.fn(async () => 'ok');
    await runCli(['test', '--module', 'fake:agent', '--prompt', 'hi'], {
      test: {
        loadAgent: async () => ({}),
        runOnce: runOnceSpy,
        readConfig: () => ({ model: 'databricks-fallback' }),
        exit: vi.fn(),
      },
    });
    expect(runOnceSpy).toHaveBeenCalledWith(
      expect.objectContaining({ model: 'databricks-fallback' }),
    );
  });
});

// ---------------------------------------------------------------------------
// list
// ---------------------------------------------------------------------------

describe('apx list', () => {
  it('calls discoverTopology with the loaded workspace client', async () => {
    const fakeModel: RegisteredModel = {
      name: 'my_agent',
      catalogName: 'main',
      schemaName: 'agents',
      fullName: 'main.agents.my_agent',
      tags: [
        { key: 'apx.agent.name', value: 'support-bot' },
        { key: 'apx.agent.model', value: 'databricks-claude-sonnet-4-6' },
        { key: 'apx.agent.tool_count', value: '3' },
      ],
    };
    const fakeTopo: Topology = {
      nodes: [
        Object.freeze({
          name: 'support-bot',
          ucName: 'main.agents.my_agent',
          modelEndpoint: 'databricks-claude-sonnet-4-6',
          toolCount: 3,
          resourceKinds: Object.freeze([]),
        }),
      ],
      edges: [],
    };
    const discoverSpy = vi.fn(async () => fakeTopo);

    const fakeWs = {
      registeredModels: {
        list: async () => [fakeModel],
      },
    };

    await runCli(
      ['list', '--workspace-client-module', 'fake:ws'],
      {
        list: {
          loadAgent: async () => fakeWs,
          discoverTopology: discoverSpy,
        },
      },
    );

    expect(discoverSpy).toHaveBeenCalledWith(
      expect.objectContaining({ registeredModelsClient: fakeWs.registeredModels }),
    );
    expect(stdout()).toContain('support-bot');
    expect(stdout()).toContain('main.agents.my_agent');
  });

  it('emits empty-state message when no nodes found', async () => {
    const fakeWs = { registeredModels: { list: async () => [] } };
    await runCli(['list', '--workspace-client-module', 'fake:ws'], {
      list: {
        loadAgent: async () => fakeWs,
        discoverTopology: async () => ({ nodes: [], edges: [] }),
      },
    });
    expect(stdout()).toContain('No apx-tagged registered models found.');
  });
});

// ---------------------------------------------------------------------------
// pyproject parser
// ---------------------------------------------------------------------------

describe('readApxAgentConfig', () => {
  it('reads keys under [tool.apx.agent] from a fixture file', () => {
    const dir = mkdtempSync(join(tmpdir(), 'apx-cli-test-'));
    try {
      const fixture = `
[project]
name = "demo"

[tool.apx.agent]
name = "demo-agent"
model = "databricks-claude-sonnet-4-6"
description = "An apx-agent."  # trailing comment
experiment = '/Shared/demo'

[tool.other]
unrelated = "skip me"
`;
      const fp = join(dir, 'pyproject.toml');
      writeFileSync(fp, fixture);
      const cfg = readApxAgentConfig(fp);
      expect(cfg.name).toBe('demo-agent');
      expect(cfg.model).toBe('databricks-claude-sonnet-4-6');
      expect(cfg.description).toBe('An apx-agent.');
      expect(cfg.experiment).toBe('/Shared/demo');
      expect((cfg as Record<string, unknown>).unrelated).toBeUndefined();
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('returns an empty object when the file is missing', () => {
    expect(readApxAgentConfig('/tmp/definitely-not-a-real-pyproject-12345.toml')).toEqual({});
  });

  it('returns an empty object when the section is absent', () => {
    expect(parseApxAgentSection('[project]\nname = "foo"\n')).toEqual({});
  });
});

// ---------------------------------------------------------------------------
// loadAgent
// ---------------------------------------------------------------------------

describe('loadAgent / parseModuleSpec', () => {
  it('parses module:variable correctly', () => {
    const parsed = parseModuleSpec('my-pkg/agent:agent');
    expect(parsed).toEqual({ module: 'my-pkg/agent', variable: 'agent' });
  });

  it('throws when missing colon', () => {
    expect(() => parseModuleSpec('no-colon')).toThrow(/MODULE:VARIABLE/);
  });

  it('throws when either side is empty', () => {
    expect(() => parseModuleSpec(':agent')).toThrow(/non-empty/);
    expect(() => parseModuleSpec('module:')).toThrow(/non-empty/);
  });

  it('resolves relative paths to file:// URLs anchored at cwd', () => {
    const resolved = resolveModuleSpecifier('./foo.js');
    expect(resolved.startsWith('file://')).toBe(true);
    expect(resolved.endsWith('/foo.js')).toBe(true);
  });

  it('leaves bare specifiers untouched for Node resolution', () => {
    expect(resolveModuleSpecifier('my-pkg/agent')).toBe('my-pkg/agent');
  });

  it('surfaces "Failed to import" when the module does not exist', async () => {
    await expect(loadAgent('./does-not-exist-12345.js:x')).rejects.toThrow(
      /Failed to import/,
    );
  });

  it('loads a real ESM module from disk and returns the named export', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'apx-cli-load-'));
    try {
      const fp = join(dir, 'agent.mjs');
      writeFileSync(fp, "export const agent = { instructions: 'hi' };\n");
      const url = pathToFileURL(fp).href;
      const result = (await loadAgent(`${url}:agent`)) as { instructions?: string };
      expect(result.instructions).toBe('hi');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
