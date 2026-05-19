/**
 * apx version — print the installed appkit-agent package version.
 *
 * Reads `package.json` next to the bundled CLI entry point. Falls back to a
 * literal `"dev"` if the file can't be read (e.g. running from source via
 * `tsx`).
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Command } from 'commander';

interface PackageJsonShape {
  version?: string;
}

function resolvePackageVersion(): string {
  // Walk up from this file until we hit a package.json with a version.
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 6; i++) {
    try {
      const raw = readFileSync(join(dir, 'package.json'), 'utf8');
      const parsed = JSON.parse(raw) as PackageJsonShape;
      if (parsed.version) return parsed.version;
    } catch {
      // Not this directory — try the parent.
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return 'dev';
}

export function registerVersionCommand(program: Command): void {
  program
    .command('version')
    .description('Print the installed appkit-agent version.')
    .action(() => {
      // Use console.log so test spies can capture it.
      // eslint-disable-next-line no-console
      console.log(resolvePackageVersion());
    });
}
