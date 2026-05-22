import Mustache from 'mustache';

import headerTpl from './templates/header.mustache?raw';
import llmTpl from './templates/llm.mustache?raw';
import sequentialTpl from './templates/sequential.mustache?raw';
import keywordRouterTpl from './templates/keyword_router.mustache?raw';
import routerTpl from './templates/router.mustache?raw';

import { AgentNodeData, EdgeData } from '../types';

const slug = (s: string) => s.replace(/[^a-zA-Z0-9_]/g, '_').replace(/^_+|_+$/g, '') || 'agent';

const varNameFor = (node: AgentNodeData): string => `agent_${slug(node.id)}`;

interface CompositionShapes {
  hasSequential: boolean;
  hasParallel: boolean;
  hasKeywordRouter: boolean;
  hasRouter: boolean;
  hasHandoff: boolean;
}

const detectShapes = (nodes: AgentNodeData[]): CompositionShapes => {
  const shapes: CompositionShapes = {
    hasSequential: false,
    hasParallel: false,
    hasKeywordRouter: false,
    hasRouter: false,
    hasHandoff: false,
  };
  for (const n of nodes) {
    if (n.type === 'supervisor' || n.type === 'group') shapes.hasSequential = true;
    if (n.type === 'router') {
      const mode = (n.config as any)?.routingMode;
      if (mode === 'keyword') shapes.hasKeywordRouter = true;
      else shapes.hasRouter = true;
    }
  }
  return shapes;
};

const childrenOf = (parentId: string, edges: EdgeData[], nodes: AgentNodeData[]): AgentNodeData[] => {
  const ids = edges.filter(e => (e as any).source === parentId).map(e => (e as any).target);
  return ids.map(id => nodes.find(n => n.id === id)).filter((n): n is AgentNodeData => !!n);
};

const renderLlm = (
  node: AgentNodeData,
  varName: string,
  edges: EdgeData[],
  nodes: AgentNodeData[]
): string => {
  const cfg = node.config as { endpointName: string; systemPrompt: string };
  // Find tool nodes connected to this LLM via outgoing edges
  const connectedToolIds = edges
    .filter(e => (e as any).source === node.id)
    .map(e => (e as any).target);
  const toolNodes = connectedToolIds
    .map(id => nodes.find(n => n.id === id))
    .filter((n): n is AgentNodeData => !!n)
    .filter(n => n.type === 'uc_function' || n.type === 'vector_search' || n.type === 'genie');

  const toolCalls = toolNodes.map(n => {
    if (n.type === 'uc_function') {
      const c = n.config as { catalog: string; schema: string; functionName: string };
      return `uc_function_tool("${c.catalog}.${c.schema}.${c.functionName}")`;
    }
    if (n.type === 'vector_search') {
      const c = n.config as { indexName: string; numResults?: number; columns?: string };
      const args: string[] = [`"${c.indexName}"`];
      if (c.numResults) args.push(`num_results=${c.numResults}`);
      if (c.columns) args.push(`columns=[${c.columns.split(',').map(s => `"${s.trim()}"`).join(', ')}]`);
      return `vector_search_tool(${args.join(', ')})`;
    }
    if (n.type === 'genie') {
      const c = n.config as { spaceId: string; description?: string };
      const args: string[] = [`"${c.spaceId}"`];
      if (c.description) args.push(`description="${c.description}"`);
      return `genie_tool(${args.join(', ')})`;
    }
    return '';
  });

  return Mustache.render(llmTpl, {
    nodeName: varName,
    systemPrompt: cfg.systemPrompt,
    tools: toolCalls,
  });
};

const renderSequential = (node: AgentNodeData, varName: string, children: AgentNodeData[], childVars: string[]): string => {
  const cfg = node.config as { description?: string };
  return Mustache.render(sequentialTpl, {
    nodeName: varName,
    agents: childVars,
    hasInstructions: !!cfg.description,
    instructions: cfg.description || '',
  });
};

const renderKeywordRouter = (node: AgentNodeData, varName: string, children: AgentNodeData[], childVars: string[]): string => {
  const cfg = node.config as any;
  const childByName = new Map<string, string>();
  children.forEach((c, i) => {
    const branchName = (c.config as any)?.branchName ?? `branch_${i}`;
    childByName.set(branchName, childVars[i]);
  });
  const branches = (cfg.branches || []).map((b: any) => ({
    name: b.name,
    agent: childByName.get(b.name) ?? childVars[0],
    keywords: b.keywords || [],
  }));
  const defaultName = cfg.defaultBranch;
  const defaultVar = defaultName ? (childByName.get(defaultName) ?? childVars[childVars.length - 1]) : childVars[childVars.length - 1];
  return Mustache.render(keywordRouterTpl, {
    nodeName: varName,
    branches,
    default: defaultVar,
  });
};

const renderRouter = (node: AgentNodeData, varName: string, children: AgentNodeData[], childVars: string[]): string => {
  const cfg = node.config as any;
  const routes = children.map((c, i) => ({
    name: (c.config as any)?.branchName ?? `route_${i}`,
    description: (c.config as any)?.branchDescription ?? '',
    agent: childVars[i],
  }));
  return Mustache.render(routerTpl, {
    nodeName: varName,
    routes,
    hasInstructions: !!cfg.description,
    instructions: cfg.description || '',
  });
};

// DFS: render children before parents so parent template refs are defined.
const renderNode = (
  node: AgentNodeData,
  edges: EdgeData[],
  nodes: AgentNodeData[],
  emitted: Set<string>,
  sections: string[]
): string => {
  const varName = varNameFor(node);
  if (emitted.has(node.id)) return varName;
  emitted.add(node.id);

  if (node.type === 'llm') {
    sections.push(renderLlm(node, varName, edges, nodes));
    return varName;
  }

  const kids = childrenOf(node.id, edges, nodes);
  const kidVars = kids.map(k => renderNode(k, edges, nodes, emitted, sections));

  if (node.type === 'supervisor' || node.type === 'group') {
    sections.push(renderSequential(node, varName, kids, kidVars));
  } else if (node.type === 'router') {
    const mode = (node.config as any)?.routingMode;
    if (mode === 'keyword') {
      sections.push(renderKeywordRouter(node, varName, kids, kidVars));
    } else {
      sections.push(renderRouter(node, varName, kids, kidVars));
    }
  }

  return varName;
};

const findRoots = (nodes: AgentNodeData[], edges: EdgeData[]): AgentNodeData[] => {
  const targets = new Set(edges.map(e => (e as any).target));
  const roots = nodes.filter(n => !targets.has(n.id));
  return roots.length > 0 ? roots : nodes;
};

export const generateAgentCode = (
  nodes: AgentNodeData[],
  edges: EdgeData[],
  agentName: string
): string => {
  const ucfNodes = nodes.filter(n => n.type === 'uc_function');
  const vsNodes = nodes.filter(n => n.type === 'vector_search');
  const lakebaseNodes = nodes.filter(n => n.type === 'lakebase');
  const genieNodes = nodes.filter((n: any) => n.type === 'genie');

  const shapes = detectShapes(nodes);
  const flags = {
    hasUCFunctions: ucfNodes.length > 0,
    hasVectorSearch: vsNodes.length > 0,
    hasGenie: genieNodes.length > 0,
    hasLakebase: lakebaseNodes.length > 0,
    ...shapes,
  };

  const sections: string[] = [];
  sections.push(Mustache.render(headerTpl, { agentName, ...flags }));

  // Lakebase placeholder — engine wiring is hand-written for v1
  if (lakebaseNodes.length > 0) {
    const lbCfg = lakebaseNodes[0].config as { instanceName?: string };
    const name = lbCfg.instanceName || 'lakebase-instance';
    sections.push(
      `# TODO: configure Lakebase engine for "${name}"\n` +
      `# Example: store = LakebaseMemoryStore(engine=engine, embedding_fn=embed, embedding_dim=1024)`
    );
  }

  const emitted = new Set<string>();
  const roots = findRoots(nodes, edges);

  // If there's only one LLM node and no composition, emit a single-Agent
  // with variable name "agent" so simple_agent test still passes.
  const llmOnly = nodes.length > 0 && nodes.every(n => n.type === 'llm' || n.type === 'uc_function' || n.type === 'vector_search' || n.type === 'genie' || n.type === 'lakebase');
  if (llmOnly && nodes.filter(n => n.type === 'llm').length === 1) {
    const llmNode = nodes.find(n => n.type === 'llm')!;
    const cfg = llmNode.config as { endpointName: string; systemPrompt: string };
    sections.push(renderLlm(llmNode, 'agent', edges, nodes));
    sections.push(`# Model: ${cfg.endpointName}`);
    return sections.join('\n\n');
  }

  for (const root of roots) {
    renderNode(root, edges, nodes, emitted, sections);
  }

  return sections.join('\n\n');
};
