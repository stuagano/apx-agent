/**
 * esbuild — bundles everything into dist/app.cjs.
 *
 * Databricks Apps npm proxy times out on large packages.
 * Bundling locally means the deployed app doesn't need npm install.
 */

import { build } from 'esbuild';

await build({
  entryPoints: ['app.ts'],
  bundle: true,
  platform: 'node',
  target: 'node20',
  format: 'cjs',
  outfile: 'dist/app.cjs',
  sourcemap: true,
  minify: false,
  keepNames: true,
  external: ['node:*', 'fsevents'],
  logLevel: 'info',
  define: { 'import.meta.url': 'undefined' },
});
