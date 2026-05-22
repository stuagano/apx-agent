import { describe, it, expect } from 'vitest';
import { generateAgentCode } from '../index';
import type { AgentNodeData } from '../../types';

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
});
