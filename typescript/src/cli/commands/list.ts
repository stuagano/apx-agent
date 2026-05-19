/**
 * apx list — discover apx-tagged registered models in the workspace.
 *
 * Bridges to {@link discoverTopology} (the same machinery `apx topology`
 * uses), then prints one row per `AgentNode`. Topology already filters to
 * registered models with the `apx.agent.name` tag, so the row set matches the
 * Python `list_cmd` exactly when the tag scheme is in place.
 *
 * # Workspace client wiring
 *
 * The Python CLI imports `databricks.sdk.WorkspaceClient` and calls
 * `ws.registered_models.list(...)` directly. TypeScript has no equivalent
 * SDK bundled with this package, so the user must supply a workspace-client
 * module via `--workspace-client-module MODULE:VARIABLE`. The exported value
 * must conform to:
 *
 *   interface WorkspaceClientLike {
 *     registeredModels: { list(opts?): AsyncIterable<RegisteredModel> | Promise<RegisteredModel[]> };
 *   }
 *
 * where `RegisteredModel` matches `src/topology.ts`'s `RegisteredModel`
 * (camelCase `catalogName`, `schemaName`, etc.). When the user's adapter
 * wraps the raw Databricks SDK (snake_case), it must translate the keys —
 * see the topology tests for a faithful fake.
 */

import type { Command } from 'commander';
import { z } from 'zod';

import { loadAgent } from '../load-agent.js';
import {
  discoverTopology as defaultDiscoverTopology,
  type RegisteredModelsClient,
  type Topology,
} from '../../topology.js';

interface WorkspaceClientLike {
  registeredModels: RegisteredModelsClient;
}

function isWorkspaceClient(value: unknown): value is WorkspaceClientLike {
  return (
    typeof value === 'object' &&
    value !== null &&
    'registeredModels' in value &&
    typeof (value as WorkspaceClientLike).registeredModels?.list === 'function'
  );
}

const ListOptionsSchema = z.object({
  workspaceClientModule: z.string().min(1),
  catalog: z.string().optional(),
  schema: z.string().optional(),
  format: z.enum(['text', 'json']).default('text'),
});

function renderText(topo: Topology): string {
  if (topo.nodes.length === 0) return 'No apx-tagged registered models found.';
  const lines: string[] = [];
  lines.push(
    `${'AGENT'.padEnd(28)}  ${'UC NAME'.padEnd(40)}  ${'MODEL'.padEnd(28)}  ${'TOOLS'.padStart(6)}`,
  );
  for (const n of topo.nodes) {
    const name = (n.name || '-').padEnd(28);
    const uc = (n.ucName || '-').padEnd(40);
    const model = (n.modelEndpoint || '-').padEnd(28);
    const tools = String(n.toolCount ?? '-').padStart(6);
    lines.push(`${name}  ${uc}  ${model}  ${tools}`);
  }
  return lines.join('\n');
}

export interface ListCommandDeps {
  /** Override for tests — defaults to the real {@link loadAgent}. */
  loadAgent?: (spec: string) => Promise<unknown>;
  /** Override topology discovery — defaults to the real implementation. */
  discoverTopology?: (opts: {
    registeredModelsClient: RegisteredModelsClient;
    catalog?: string;
    schema?: string;
  }) => Promise<Topology>;
}

export function registerListCommand(program: Command, deps: ListCommandDeps = {}): void {
  const load = deps.loadAgent ?? loadAgent;
  const discover = deps.discoverTopology ?? defaultDiscoverTopology;

  program
    .command('list')
    .description('Discover apx-tagged registered models via a user-supplied WorkspaceClient.')
    .requiredOption(
      '--workspace-client-module <spec>',
      'Module exporting a WorkspaceClient-shaped object, "module:variable".',
    )
    .option('--catalog <name>', 'Restrict to a UC catalog.')
    .option('--schema <name>', 'Restrict to a UC schema (requires --catalog).')
    .option('--format <fmt>', 'Output format (text|json).', 'text')
    .action(
      async (rawOpts: {
        workspaceClientModule: string;
        catalog?: string;
        schema?: string;
        format: string;
      }) => {
        const opts = ListOptionsSchema.parse(rawOpts);
        if (opts.schema && !opts.catalog) {
          throw new Error('--schema requires --catalog.');
        }
        const ws = await load(opts.workspaceClientModule);
        if (!isWorkspaceClient(ws)) {
          throw new Error(
            `Module ${JSON.stringify(opts.workspaceClientModule)} did not export a ` +
              `WorkspaceClient-shaped object (expected .registeredModels.list).`,
          );
        }
        const topo = await discover({
          registeredModelsClient: ws.registeredModels,
          ...(opts.catalog ? { catalog: opts.catalog } : {}),
          ...(opts.schema ? { schema: opts.schema } : {}),
        });

        if (opts.format === 'json') {
          // eslint-disable-next-line no-console
          console.log(JSON.stringify({ nodes: topo.nodes, edges: topo.edges }, null, 2));
        } else {
          // eslint-disable-next-line no-console
          console.log(renderText(topo));
        }
      },
    );
}
