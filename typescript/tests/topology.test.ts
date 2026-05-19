/**
 * Tests for topology.ts — discover + render the multi-agent graph.
 *
 * Mirrors python/tests/test_topology.py.
 */

import { describe, it, expect } from 'vitest';
import {
  discoverTopology,
  renderTopology,
  type RegisteredModel,
  type RegisteredModelsClient,
  type Topology,
} from '../src/topology.js';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function model(
  name: string,
  tags: Record<string, string>,
  opts: { catalog?: string; schema?: string } = {},
): RegisteredModel {
  const catalog = opts.catalog ?? 'main';
  const schema = opts.schema ?? 'agents';
  return {
    name,
    catalogName: catalog,
    schemaName: schema,
    fullName: `${catalog}.${schema}.${name}`,
    tags: Object.entries(tags).map(([key, value]) => ({ key, value })),
  };
}

function fakeClient(models: RegisteredModel[]): RegisteredModelsClient {
  return {
    list: async () => models,
  };
}

function sampleTopology(): Topology {
  return {
    nodes: [
      {
        name: 'triage',
        ucName: 'main.agents.triage',
        modelEndpoint: 'databricks-claude-sonnet-4-6',
        toolCount: 3,
        resourceKinds: ['uc_function', 'genie_space'],
      },
      {
        name: 'billing',
        ucName: 'main.agents.billing',
        modelEndpoint: 'databricks-claude-sonnet-4-6',
        resourceKinds: [],
      },
    ],
    edges: [
      { source: 'triage', target: 'billing', targetKind: 'endpoint' },
      { source: 'triage', target: 'external_lookup', targetKind: 'endpoint' },
    ],
  };
}

// ---------------------------------------------------------------------------
// discoverTopology
// ---------------------------------------------------------------------------

describe('discoverTopology', () => {
  it('pulls AgentNodes from apx-tagged models and skips untagged ones', async () => {
    const client = fakeClient([
      model('triage', {
        'apx.agent.name': 'triage',
        'apx.agent.model': 'databricks-claude-sonnet-4-6',
        'apx.agent.tool_count': '3',
        'apx.agent.metadata': JSON.stringify({
          sub_agents: [],
          resources: [{ kind: 'uc_function', identifier: 'main.tools.x' }],
        }),
      }),
      model('untagged', { 'other.tag': 'x' }),
    ]);

    const topo = await discoverTopology({ registeredModelsClient: client });

    expect(topo.nodes).toHaveLength(1);
    const node = topo.nodes[0];
    expect(node.name).toBe('triage');
    expect(node.modelEndpoint).toBe('databricks-claude-sonnet-4-6');
    expect(node.toolCount).toBe(3);
    expect(node.resourceKinds).toContain('uc_function');
  });

  it('extracts edges from metadata.sub_agents (endpoint refs)', async () => {
    const client = fakeClient([
      model('triage', {
        'apx.agent.name': 'triage',
        'apx.agent.metadata': JSON.stringify({
          sub_agents: ['endpoints/billing', 'serving-endpoints/technical', 'data-triage'],
        }),
      }),
    ]);

    const topo = await discoverTopology({ registeredModelsClient: client });

    expect(topo.edges).toHaveLength(3);
    const targetNames = new Set(topo.edges.map((e) => e.target));
    expect(targetNames).toEqual(new Set(['billing', 'technical', 'data-triage']));
    for (const e of topo.edges) {
      expect(e.source).toBe('triage');
      expect(e.targetKind).toBe('endpoint');
    }
  });

  it('classifies Apps URLs separately as app_url', async () => {
    const client = fakeClient([
      model('triage', {
        'apx.agent.name': 'triage',
        'apx.agent.metadata': JSON.stringify({
          sub_agents: ['https://billing.workspace.databricksapps.com'],
        }),
      }),
    ]);

    const topo = await discoverTopology({ registeredModelsClient: client });

    expect(topo.edges).toHaveLength(1);
    const e = topo.edges[0];
    expect(e.targetKind).toBe('app_url');
    expect(e.target).toContain('billing.workspace.databricksapps.com');
  });

  it('falls back to comma-separated apx.agent.sub_agents tag when metadata lacks sub_agents', async () => {
    const client = fakeClient([
      model('triage', {
        'apx.agent.name': 'triage',
        'apx.agent.sub_agents': 'endpoints/billing,endpoints/technical',
      }),
    ]);

    const topo = await discoverTopology({ registeredModelsClient: client });

    const targetNames = new Set(topo.edges.map((e) => e.target));
    expect(targetNames).toEqual(new Set(['billing', 'technical']));
  });

  it('tolerates malformed metadata JSON — model still appears as node, no edges', async () => {
    const client = fakeClient([
      model('triage', {
        'apx.agent.name': 'triage',
        'apx.agent.metadata': 'not valid json[',
      }),
    ]);

    const topo = await discoverTopology({ registeredModelsClient: client });
    expect(topo.nodes).toHaveLength(1);
    expect(topo.edges).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// renderTopology
// ---------------------------------------------------------------------------

describe('renderTopology', () => {
  it('mermaid output includes nodes, edges, and external targets', () => {
    const text = renderTopology(sampleTopology(), { format: 'mermaid' });
    expect(text.startsWith('graph LR')).toBe(true);
    expect(text).toContain('triage');
    expect(text).toContain('billing');
    expect(text).toContain('-->');
    expect(text).toContain('external_lookup');
  });

  it('mermaid embeds model endpoint as subtext', () => {
    const text = renderTopology(sampleTopology(), { format: 'mermaid' });
    expect(text).toContain('databricks-claude-sonnet-4-6');
    expect(text).toContain('<small>');
  });

  it('graphviz output starts with digraph and has labeled nodes + directed edges', () => {
    const text = renderTopology(sampleTopology(), { format: 'graphviz' });
    expect(text.startsWith('digraph apx_topology')).toBe(true);
    expect(text).toContain('"triage"');
    expect(text).toContain('"billing"');
    expect(text).toContain('"triage" -> "billing"');
  });

  it('rejects unknown formats', () => {
    expect(() =>
      renderTopology(sampleTopology(), { format: 'svg' as unknown as 'mermaid' }),
    ).toThrow(/Unknown format/);
  });

  it('handles empty topology in both formats', () => {
    const empty: Topology = { nodes: [], edges: [] };
    const mermaid = renderTopology(empty, { format: 'mermaid' });
    expect(mermaid.startsWith('graph LR')).toBe(true);
    const graphviz = renderTopology(empty, { format: 'graphviz' });
    expect(graphviz.startsWith('digraph apx_topology')).toBe(true);
  });
});
