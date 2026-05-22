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
});
