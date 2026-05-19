/**
 * tsdown config — bundle both the library entry and the CLI entry.
 *
 * Two entries:
 *   - `src/index.ts`     → `dist/index.mjs` (the library)
 *   - `src/cli/index.ts` → `dist/cli/index.mjs` (the `apx` CLI binary)
 *
 * The CLI entry preserves its shebang so it stays executable when published
 * — npm wires up the `bin` field via a symlink that runs the file directly.
 */

import { defineConfig } from 'tsdown';

export default defineConfig({
  entry: {
    index: 'src/index.ts',
    'cli/index': 'src/cli/index.ts',
  },
  outDir: 'dist',
  format: 'esm',
  dts: true,
  sourcemap: true,
  clean: true,
  platform: 'node',
});
