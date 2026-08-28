/**
 * Topology visualization — render the multi-agent endpoint graph.
 *
 * Scans Unity Catalog for registered models tagged with ``apx.agent.name``
 * (the tags the deploy step writes after registering an agent), parses the
 * ``apx.agent.metadata`` JSON blob to extract declared sub-agent endpoints,
 * and produces a directed graph in either Mermaid or Graphviz syntax.
 *
 * The graph shows agent-to-agent edges so you can answer "what calls what"
 * without inspecting individual agent code or stitching the answer together
 * from system tables. Combined with the cost report, this is the production
 * view of the multi-agent estate.
 *
 * @example
 * import { discoverTopology, renderTopology } from 'apx-internal-runtime';
 *
 * const topo = await discoverTopology({ registeredModelsClient: myClient });
 * console.log(renderTopology(topo, { format: 'mermaid' }));
 *
 * The ``registeredModelsClient`` parameter mirrors the canary
 * ``servingEndpointsClient`` pattern — a thin protocol with one ``list()``
 * method. Implementations may return either an ``AsyncIterable`` or a
 * ``Promise<RegisteredModel[]>``; tests pass a hand-rolled fake.
 */

import { z } from 'zod';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** A UC tag — key/value pair on a registered model. */
export interface RegisteredModelTag {
  key: string;
  value: string;
}

/** One Unity Catalog registered model, as returned by the SDK. */
export interface RegisteredModel {
  name: string;
  catalogName: string;
  schemaName: string;
  fullName?: string;
  tags?: ReadonlyArray<RegisteredModelTag>;
}

/**
 * Client capable of listing UC registered models.
 *
 * Mirrors the canary ``servingEndpointsClient`` pattern — a single ``list()``
 * method that may return either an ``AsyncIterable`` (paginated SDKs) or a
 * ``Promise<Array>`` (REST-list shims). Tests pass a fake; production code
 * passes a thin wrapper over the Databricks SDK.
 */
export interface RegisteredModelsClient {
  list(options?: {
    catalogName?: string;
    schemaName?: string;
  }): AsyncIterable<RegisteredModel> | Promise<ReadonlyArray<RegisteredModel>>;
}

/** One agent in the topology graph. */
export interface AgentNode {
  readonly name: string;
  readonly ucName: string;
  readonly modelEndpoint?: string;
  readonly toolCount?: number;
  readonly resourceKinds: readonly string[];
}

/** A directed edge from one agent to another it can call. */
export interface TopologyEdge {
  readonly source: string;
  readonly target: string;
  readonly targetKind: 'endpoint' | 'app_url' | 'unresolved';
}

/** Collected agents + edges discovered from UC tags. */
export interface Topology {
  readonly nodes: readonly AgentNode[];
  readonly edges: readonly TopologyEdge[];
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------

const DiscoverOptionsSchema = z.object({
  registeredModelsClient: z.custom<RegisteredModelsClient>(
    (v) => !!v && typeof (v as RegisteredModelsClient).list === 'function',
    { message: 'registeredModelsClient must expose a list() method' },
  ),
  catalog: z.string().optional(),
  schema: z.string().optional(),
});

export interface DiscoverTopologyOptions {
  /** Client used to list registered models — mirrors canary's servingEndpointsClient. */
  registeredModelsClient: RegisteredModelsClient;
  /** Optional UC catalog filter. */
  catalog?: string;
  /** Optional UC schema filter (requires ``catalog``). */
  schema?: string;
}

/**
 * Walk UC registered models to build the agent-to-agent graph.
 *
 * Returns a ``Topology`` with one ``AgentNode`` per discovered agent and one
 * ``TopologyEdge`` per declared sub-agent link. Sub-agents that appear as
 * edge targets but don't have their own ``apx.agent.name`` tag stay in the
 * graph as targets without a corresponding node — useful for spotting
 * external dependencies (Apps URLs, ad-hoc endpoint references).
 */
export async function discoverTopology(options: DiscoverTopologyOptions): Promise<Topology> {
  const parsed = DiscoverOptionsSchema.parse(options);
  const client = parsed.registeredModelsClient;

  const models = await collectModels(client, { catalogName: parsed.catalog, schemaName: parsed.schema });

  const nodes: AgentNode[] = [];
  const edges: TopologyEdge[] = [];

  for (const m of models) {
    const tags = tagsToMap(m.tags);
    const agentName = tags.get('apx.agent.name');
    if (!agentName) continue;

    const ucName =
      m.fullName ??
      `${m.catalogName ?? ''}.${m.schemaName ?? ''}.${m.name ?? ''}`;

    const toolCount = coerceInt(tags.get('apx.agent.tool_count'));
    const resourceKinds = resourceKindsFromMetadata(tags.get('apx.agent.metadata'));

    const node: AgentNode = Object.freeze({
      name: agentName,
      ucName,
      modelEndpoint: tags.get('apx.agent.model'),
      toolCount,
      resourceKinds: Object.freeze([...resourceKinds].sort()),
    });
    nodes.push(node);

    // Prefer metadata.sub_agents (URLs/names); fall back to comma-separated tag.
    let subAgents = subAgentsFromMetadata(tags.get('apx.agent.metadata'));
    if (subAgents.length === 0) {
      const csv = tags.get('apx.agent.sub_agents') ?? '';
      subAgents = csv.split(',').map((s) => s.trim()).filter((s) => s.length > 0);
    }
    for (const raw of subAgents) {
      const { target, kind } = classifySubAgent(raw);
      edges.push(Object.freeze({ source: agentName, target, targetKind: kind }));
    }
  }

  return Object.freeze({
    nodes: Object.freeze(nodes),
    edges: Object.freeze(edges),
  });
}

async function collectModels(
  client: RegisteredModelsClient,
  options: { catalogName?: string; schemaName?: string },
): Promise<RegisteredModel[]> {
  const result = client.list(options);
  // Promise<Array> form
  if (typeof (result as Promise<unknown>).then === 'function') {
    const arr = await (result as Promise<ReadonlyArray<RegisteredModel>>);
    return [...arr];
  }
  // AsyncIterable form
  const out: RegisteredModel[] = [];
  for await (const m of result as AsyncIterable<RegisteredModel>) {
    out.push(m);
  }
  return out;
}

function tagsToMap(tags?: ReadonlyArray<RegisteredModelTag>): Map<string, string> {
  const m = new Map<string, string>();
  for (const t of tags ?? []) {
    m.set(t.key, t.value);
  }
  return m;
}

function resourceKindsFromMetadata(metadataJson?: string): string[] {
  if (!metadataJson) return [];
  try {
    const parsed = JSON.parse(metadataJson) as { resources?: Array<{ kind?: string }> };
    const kinds = new Set<string>();
    for (const r of parsed.resources ?? []) {
      if (r?.kind) kinds.add(r.kind);
    }
    return [...kinds].sort();
  } catch {
    return [];
  }
}

function subAgentsFromMetadata(metadataJson?: string): string[] {
  if (!metadataJson) return [];
  try {
    const parsed = JSON.parse(metadataJson) as { sub_agents?: unknown[] };
    return (parsed.sub_agents ?? []).map((s) => String(s));
  } catch {
    return [];
  }
}

function classifySubAgent(raw: string): { target: string; kind: 'endpoint' | 'app_url' | 'unresolved' } {
  const s = raw.trim();
  if (s.includes('databricksapps.com')) {
    return { target: s, kind: 'app_url' };
  }
  if (s.startsWith('endpoints/')) {
    return { target: s.slice('endpoints/'.length), kind: 'endpoint' };
  }
  if (s.startsWith('serving-endpoints/')) {
    return { target: s.slice('serving-endpoints/'.length), kind: 'endpoint' };
  }
  if (s.includes('://')) {
    return { target: s, kind: 'unresolved' };
  }
  return { target: s, kind: 'endpoint' };
}

function coerceInt(value: string | undefined): number | undefined {
  if (value === undefined || value === '') return undefined;
  const n = Number(value);
  if (!Number.isFinite(n) || !Number.isInteger(n)) {
    // Try parseInt for non-integer numeric strings (e.g. "3.0")
    const i = parseInt(value, 10);
    return Number.isNaN(i) ? undefined : i;
  }
  return n;
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

const RenderOptionsSchema = z.object({
  format: z.enum(['mermaid', 'graphviz']).default('mermaid'),
});

export interface RenderTopologyOptions {
  format?: 'mermaid' | 'graphviz';
}

/**
 * Render a ``Topology`` as a graph diagram.
 *
 * @param topology - Output of ``discoverTopology``.
 * @param options  - ``format``: ``'mermaid'`` (default, Markdown-friendly) or
 *                   ``'graphviz'`` (DOT for ``dot``-tooling).
 */
export function renderTopology(topology: Topology, options: RenderTopologyOptions = {}): string {
  let format: 'mermaid' | 'graphviz';
  try {
    format = RenderOptionsSchema.parse(options).format;
  } catch {
    throw new Error(
      `Unknown format ${JSON.stringify((options as { format?: unknown }).format)}; expected 'mermaid' or 'graphviz'.`,
    );
  }
  if (format === 'mermaid') return renderMermaid(topology);
  return renderGraphviz(topology);
}

function renderMermaid(topology: Topology): string {
  const lines: string[] = ['graph LR'];
  const nodeIds = new Map<string, string>();

  for (const node of topology.nodes) {
    const id = safeId(node.name);
    nodeIds.set(node.name, id);
    let label = node.name;
    if (node.modelEndpoint) {
      label += `<br/><small>${node.modelEndpoint}</small>`;
    }
    lines.push(`  ${id}["${label}"]`);
  }

  const seenExternal = new Set<string>();
  for (const edge of topology.edges) {
    const srcId = nodeIds.get(edge.source) ?? safeId(edge.source);
    if (!nodeIds.has(edge.target) && !seenExternal.has(edge.target)) {
      const tgtId = safeId(edge.target);
      const [open, close] = edge.targetKind === 'endpoint' ? ['(', ')'] : ['[', ']'];
      lines.push(`  ${tgtId}${open}"${edge.target}"${close}`);
      seenExternal.add(edge.target);
    }
    const tgtId = nodeIds.get(edge.target) ?? safeId(edge.target);
    lines.push(`  ${srcId} --> ${tgtId}`);
  }

  return lines.join('\n');
}

function renderGraphviz(topology: Topology): string {
  const lines: string[] = ['digraph apx_topology {', '  rankdir=LR;', '  node [shape=box];'];
  for (const node of topology.nodes) {
    let label = node.name;
    if (node.modelEndpoint) {
      label += `\\n${node.modelEndpoint}`;
    }
    lines.push(`  "${node.name}" [label="${label}"];`);
  }

  const nodeNames = new Set(topology.nodes.map((n) => n.name));
  const seenExternal = new Set<string>();
  for (const edge of topology.edges) {
    if (!nodeNames.has(edge.target) && !seenExternal.has(edge.target)) {
      const shape = edge.targetKind === 'endpoint' ? 'ellipse' : 'note';
      lines.push(`  "${edge.target}" [shape=${shape}];`);
      seenExternal.add(edge.target);
    }
    lines.push(`  "${edge.source}" -> "${edge.target}";`);
  }
  lines.push('}');
  return lines.join('\n');
}

function safeId(name: string): string {
  const cleaned = name.replace(/\W+/g, '_').replace(/^_+|_+$/g, '');
  return cleaned || 'n';
}
