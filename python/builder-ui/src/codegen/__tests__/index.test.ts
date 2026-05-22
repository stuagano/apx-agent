import { describe, it, expect } from 'vitest';
import { generateAgentCode } from '../index';
import type { AgentNodeData, EdgeData } from '../../types';

describe('generateAgentCode (apx-agent target)', () => {
  it('emits a single Agent for one LLM node + no tools', () => {
    const nodes: AgentNodeData[] = [
      {
        id: 'n1',
        type: 'llm',
        config: {
          endpointName: 'databricks-claude-sonnet-4-6',
          systemPrompt: 'You are a helpful assistant.',
          model: '',
          maxTokens: 1024,
          temperature: 0.0,
          maxIterations: 10,
        },
        position: { x: 0, y: 0 },
      } as any,
    ];
    const code = generateAgentCode(nodes, [], 'simple_agent');
    expect(code).toContain('from apx_agent import');
    expect(code).toContain('Agent');
    expect(code).toContain('You are a helpful assistant.');
    expect(code).toContain('databricks-claude-sonnet-4-6');
    // Should NOT emit the old raw-LangGraph scaffolding
    expect(code).not.toContain('class AgentState(TypedDict)');
    expect(code).not.toContain('create_tool_calling_agent');
  });

  it('emits SequentialAgent for a supervisor node with ordered children', () => {
    const nodes: AgentNodeData[] = [
      { id: 's', type: 'supervisor', config: { description: 'pipeline' }, position: { x: 0, y: 0 } } as any,
      { id: 'a', type: 'llm', config: { endpointName: 'x', systemPrompt: 'step a' }, position: { x: 0, y: 0 } } as any,
      { id: 'b', type: 'llm', config: { endpointName: 'x', systemPrompt: 'step b' }, position: { x: 0, y: 0 } } as any,
    ];
    const edges: EdgeData[] = [
      { source: 's', target: 'a' } as any,
      { source: 's', target: 'b' } as any,
    ];
    const code = generateAgentCode(nodes, edges, 'pipe');
    expect(code).toContain('SequentialAgent(');
    expect(code).toContain('step a');
    expect(code).toContain('step b');
    expect(code).toContain('from apx_agent import');
    expect(code).toMatch(/from apx_agent import[\s\S]*SequentialAgent/);
  });

  it('emits KeywordRouter for a router node with routingMode=keyword', () => {
    const nodes: AgentNodeData[] = [
      {
        id: 'r', type: 'router',
        config: {
          description: 'route',
          routingMode: 'keyword',
          branches: [{ name: 'investigate', keywords: ['missing', 'investigate'] }],
          defaultBranch: 'general',
        },
        position: { x: 0, y: 0 },
      } as any,
      { id: 'inv', type: 'llm', config: { endpointName: 'x', systemPrompt: 'inv', branchName: 'investigate' }, position: { x: 0, y: 0 } } as any,
      { id: 'gen', type: 'llm', config: { endpointName: 'x', systemPrompt: 'gen', branchName: 'general' }, position: { x: 0, y: 0 } } as any,
    ];
    const edges: EdgeData[] = [
      { source: 'r', target: 'inv' } as any,
      { source: 'r', target: 'gen' } as any,
    ];
    const code = generateAgentCode(nodes, edges, 'kr');
    expect(code).toContain('KeywordRouter(');
    expect(code).toContain('"missing"');
    expect(code).toContain('"investigate"');
    expect(code).toContain('default=');
    expect(code).toMatch(/from apx_agent import[\s\S]*KeywordRouter/);
  });

  it('emits RouterAgent for a router node with routingMode=llm (or default)', () => {
    const nodes: AgentNodeData[] = [
      {
        id: 'r', type: 'router',
        config: { description: 'route', routingMode: 'llm' },
        position: { x: 0, y: 0 },
      } as any,
      { id: 'bill', type: 'llm', config: { endpointName: 'x', systemPrompt: 'bill', branchName: 'billing', branchDescription: 'Billing questions' }, position: { x: 0, y: 0 } } as any,
      { id: 'tech', type: 'llm', config: { endpointName: 'x', systemPrompt: 'tech', branchName: 'tech', branchDescription: 'Tech questions' }, position: { x: 0, y: 0 } } as any,
    ];
    const edges: EdgeData[] = [
      { source: 'r', target: 'bill' } as any,
      { source: 'r', target: 'tech' } as any,
    ];
    const code = generateAgentCode(nodes, edges, 'lr');
    expect(code).toContain('RouterAgent(');
    expect(code).toMatch(/from apx_agent import[\s\S]*RouterAgent/);
  });

  it('inlines uc_function_tool factories into the LLM tools=[] list', () => {
    const nodes: AgentNodeData[] = [
      { id: 'l', type: 'llm', config: { endpointName: 'x', systemPrompt: 'use the tool' }, position: { x: 0, y: 0 } } as any,
      { id: 't1', type: 'uc_function', config: { catalog: 'main', schema: 'tools', functionName: 'classify_intent', description: 'classify' }, position: { x: 0, y: 0 } } as any,
    ];
    const edges: EdgeData[] = [{ source: 'l', target: 't1' } as any];
    const code = generateAgentCode(nodes, edges, 'a');
    expect(code).toContain('uc_function_tool("main.tools.classify_intent")');
    // Inlined into the LLM Agent's tools list, not emitted as a separate variable
    expect(code).toMatch(/tools=\[[^\]]*uc_function_tool\("main\.tools\.classify_intent"\)/);
    expect(code).toMatch(/from apx_agent import[\s\S]*uc_function_tool/);
  });

  it('inlines vector_search_tool factories', () => {
    const nodes: AgentNodeData[] = [
      { id: 'l', type: 'llm', config: { endpointName: 'x', systemPrompt: 's' }, position: { x: 0, y: 0 } } as any,
      { id: 'v', type: 'vector_search', config: { endpointName: 'vs', indexName: 'main.search.docs', columns: 'doc_id,title,content', numResults: 5 }, position: { x: 0, y: 0 } } as any,
    ];
    const edges: EdgeData[] = [{ source: 'l', target: 'v' } as any];
    const code = generateAgentCode(nodes, edges, 'a');
    expect(code).toContain('vector_search_tool("main.search.docs"');
    expect(code).toMatch(/tools=\[[^\]]*vector_search_tool/);
    expect(code).toMatch(/from apx_agent import[\s\S]*vector_search_tool/);
  });

  it('emits genie_tool when a genie node is wired to an LLM', () => {
    const nodes: AgentNodeData[] = [
      { id: 'l', type: 'llm', config: { endpointName: 'x', systemPrompt: 's' }, position: { x: 0, y: 0 } } as any,
      { id: 'g', type: 'genie', config: { spaceId: 'abc123', description: 'sales questions' }, position: { x: 0, y: 0 } } as any,
    ];
    const edges: EdgeData[] = [{ source: 'l', target: 'g' } as any];
    const code = generateAgentCode(nodes, edges, 'a');
    expect(code).toContain('genie_tool("abc123"');
    expect(code).toMatch(/from apx_agent import[\s\S]*genie_tool/);
  });

  it('emits a Lakebase placeholder when a lakebase node is present', () => {
    const nodes: AgentNodeData[] = [
      { id: 'l', type: 'llm', config: { endpointName: 'x', systemPrompt: 's' }, position: { x: 0, y: 0 } } as any,
      { id: 'lb', type: 'lakebase', config: { instanceName: 'prod-lakebase' }, position: { x: 0, y: 0 } } as any,
    ];
    const edges: EdgeData[] = [];
    const code = generateAgentCode(nodes, edges, 'a');
    expect(code).toMatch(/from apx_agent import[\s\S]*LakebaseMemoryStore/);
    expect(code).toContain('# TODO: configure Lakebase engine for "prod-lakebase"');
  });
});
