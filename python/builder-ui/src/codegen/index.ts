import Mustache from 'mustache';
import { AgentNodeData, EdgeData } from '../types';

import headerTpl       from './templates/header.mustache?raw';
import llmTpl          from './templates/llm.mustache?raw';
import vectorSearchTpl from './templates/vectorSearch.mustache?raw';
import ucFunctionsTpl  from './templates/ucFunctions.mustache?raw';
import agentTpl        from './templates/agent.mustache?raw';
import registrationTpl from './templates/registration.mustache?raw';

export const generateAgentCode = (
  nodes: AgentNodeData[],
  _edges: EdgeData[],
  agentName: string
): string => {
  const agentNode  = nodes.find(n => n.type === 'supervisor' || n.type === 'router');
  const llmNodes   = nodes.filter(n => n.type === 'llm');
  const vsNodes    = nodes.filter(n => n.type === 'vector_search');
  const ucfNodes   = nodes.filter(n => n.type === 'uc_function');

  const hasVectorSearch = vsNodes.length > 0;
  const hasUCFunctions  = ucfNodes.length > 0;

  const sections: string[] = [];

  // 1. Header + imports (needs flags for conditional imports)
  sections.push(Mustache.render(headerTpl, { agentName, hasVectorSearch, hasUCFunctions }));

  // 2. LLM
  if (llmNodes.length > 0) {
    const cfg = llmNodes[0].config as { endpointName: string; systemPrompt: string };
    sections.push(Mustache.render(llmTpl, cfg));
  }

  // 3. UC Functions
  if (hasUCFunctions) {
    const tools = ucfNodes.map(ucf => {
      const cfg = ucf.config as { catalog: string; schema: string; functionName: string; description: string };
      return {
        fullName:    `${cfg.catalog}.${cfg.schema}.${cfg.functionName}`,
        description: cfg.description || ucf.label,
      };
    });
    sections.push(Mustache.render(ucFunctionsTpl, { tools }));
  }

  // 4. Vector Search
  if (hasVectorSearch) {
    const retrievers = vsNodes.map(vs => {
      const cfg = vs.config as { endpointName: string; indexName: string; textColumn: string; columns: string; numResults: number };
      return {
        varName:      vs.label.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, ''),
        endpointName: cfg.endpointName,
        indexName:    cfg.indexName,
        columns:      cfg.columns.split(',').map(c => `"${c.trim()}"`).join(', '),
        numResults:   cfg.numResults,
      };
    });
    sections.push(Mustache.render(vectorSearchTpl, { retrievers }));
  }

  // 5. Agent class (LangGraph + ResponsesAgent boilerplate)
  const agentCfg = agentNode
    ? (agentNode.config as { description: string; maxIterations: number })
    : { description: 'A Databricks AI agent', maxIterations: 10 };

  sections.push(Mustache.render(agentTpl, {
    agentName,
    description:   agentCfg.description,
    maxIterations: agentCfg.maxIterations,
  }));

  // 6. Assemble tools + set_model
  sections.push(Mustache.render(registrationTpl, { agentName, hasVectorSearch, hasUCFunctions }));

  return sections.join('\n');
};
