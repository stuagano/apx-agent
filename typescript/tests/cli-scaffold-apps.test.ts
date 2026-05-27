/**
 * Tests for `apx scaffold --target apps`.
 *
 * Covers the Apps-target scaffold path. Side-effecting fs calls are stubbed
 * via the `ScaffoldCommandDeps` injection seam exposed by
 * `registerScaffoldCommand`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mkdtempSync, rmSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { buildProgram } from '../src/cli/index.js';

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

async function runCli(args: string[], deps: Parameters<typeof buildProgram>[0] = {}) {
  const program = buildProgram(deps);
  await program.parseAsync(['node', 'apx', ...args]);
}

describe('apx scaffold --target apps', () => {
  it('writes the expected file set for the apps target', async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'apx-scaffold-apps-'));
    try {
      await runCli(['scaffold', 'my_app', '--dir', tmp, '--target', 'apps']);
      const root = join(tmp, 'my_app');
      const expected = [
        'package.json',
        'tsconfig.json',
        'databricks.yml',
        '.env.example',
        '.gitignore',
        'README.md',
        'agent_server/agent.ts',
        'agent_server/index.ts',
        'agent_server/start.ts',
      ];
      for (const rel of expected) {
        expect(existsSync(join(root, rel)), `${rel} should exist`).toBe(true);
      }
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it('substitutes the project name into generated package.json + databricks.yml', async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'apx-scaffold-apps-'));
    try {
      await runCli(['scaffold', 'cool_app', '--dir', tmp, '--target', 'apps']);
      const pkg = JSON.parse(
        readFileSync(join(tmp, 'cool_app', 'package.json'), 'utf8'),
      );
      expect(pkg.name).toBe('cool_app');
      // appkit-agent isn't published to npm yet — the scaffold must reference
      // the local framework build, not a registry version that 404s.
      expect(pkg.dependencies?.['appkit-agent']).toBe('file:..');

      const yml = readFileSync(join(tmp, 'cool_app', 'databricks.yml'), 'utf8');
      expect(yml).toContain('name: cool_app');
      expect(yml).toContain('resources:\n  apps:\n    cool_app:');
      expect(yml).not.toContain('<APP_NAME>');

      // tsconfig.json must parse
      const tsconfig = JSON.parse(
        readFileSync(join(tmp, 'cool_app', 'tsconfig.json'), 'utf8'),
      );
      expect(tsconfig.compilerOptions?.target).toBe('ES2022');
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it('refuses to overwrite an existing non-empty directory without --force', async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'apx-scaffold-apps-'));
    try {
      await runCli(['scaffold', 'a', '--dir', tmp, '--target', 'apps']);
      await expect(
        runCli(['scaffold', 'a', '--dir', tmp, '--target', 'apps']),
      ).rejects.toThrow(/already exists/);
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it('overwrites an existing project when --force is set', async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'apx-scaffold-apps-'));
    try {
      await runCli(['scaffold', 'a', '--dir', tmp, '--target', 'apps']);
      // Should not throw
      await runCli(['scaffold', 'a', '--dir', tmp, '--target', 'apps', '--force']);
      const yml = readFileSync(join(tmp, 'a', 'databricks.yml'), 'utf8');
      expect(yml).toContain('name: a');
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });

  it('generated start.ts uses mountResponsesAgent and listens on DATABRICKS_APP_PORT', async () => {
    const tmp = mkdtempSync(join(tmpdir(), 'apx-scaffold-apps-'));
    try {
      await runCli(['scaffold', 'pp', '--dir', tmp, '--target', 'apps']);
      const start = readFileSync(join(tmp, 'pp', 'agent_server/start.ts'), 'utf8');
      expect(start).toContain('mountResponsesAgent');
      expect(start).toContain('DATABRICKS_APP_PORT');
      expect(start).toContain("from 'appkit-agent'");
    } finally {
      rmSync(tmp, { recursive: true, force: true });
    }
  });
});
