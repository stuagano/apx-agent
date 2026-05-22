import { AgentNodeData, CICDConfig, EdgeData, LakebaseConfig, LLMConfig, ProjectSettings, RouterConfig, SupervisorConfig, UCFunctionConfig, VectorSearchConfig } from '../types';
import { DEFAULT_CICD_CONFIG } from '../constants';

// ── Config JSON ───────────────────────────────────────────────────────────────
// Maps canvas state → key-value pairs for:
//   databricks bundle init https://github.com/veenaramesh/agentops-demo \
//     --config-file config.json

const toProjectName = (agentName: string) =>
  agentName
    .toLowerCase()
    .replace(/\s+/g, '_')
    .replace(/[^a-z0-9_-]/g, '')
    .replace(/^[^a-z]+/, '') || 'my_agent_project';

const toSnakeCase = (s: string) =>
  s
    .toLowerCase()
    .replace(/\s+/g, '_')
    .replace(/[^a-z0-9_]/g, '');

// ── Shared sub-types ──────────────────────────────────────────────────────────

interface ToolDef {
  name: string;        // cfg.functionName
  catalog: string;
  schema: string;
  description: string;
  deploy: boolean;     // true = generate stub + deploy; false = call existing
}

interface RetrieverDef {
  name: string;          // toSnakeCase(node.label)
  endpoint_name: string;
  index_name: string;
  text_column: string;
  columns: string;       // comma-separated
  num_results: number;
}

interface LakebaseDef {
  name: string;          // toSnakeCase(node.label)
  instance_name: string;
  description: string;
}

interface WorkerLLMDef {
  name: string;               // toSnakeCase(node.label)
  endpoint_name: string;      // cfg.endpointName
  model: string;              // cfg.model
  max_iterations: number;     // cfg.maxIterations — controls inner ReAct loop
  tools: ToolDef[];           // ucf nodes → this worker LLM
  retrievers: RetrieverDef[]; // vs nodes → this worker LLM
  lakebase_tools: LakebaseDef[]; // lakebase nodes → this worker LLM
}

interface SupervisorLLMDef {
  endpoint_name: string;
  model: string;
  max_iterations: number;     // controls how many routing decisions the supervisor makes
}

// PipelineLLMDef: one stage in a sequential chain (llm-A → llm-B → ...)
interface PipelineLLMDef {
  name: string;               // toSnakeCase(node.label)
  endpoint_name: string;
  model: string;
  max_iterations: number;     // controls inner ReAct loop for this stage
  tools: ToolDef[];           // ucf nodes → this stage's LLM
  retrievers: RetrieverDef[]; // vs nodes → this stage's LLM
  lakebase_tools: LakebaseDef[]; // lakebase nodes → this stage's LLM
}

// ParallelBranchDef: one branch in a parallel fan-out (agent → llm-A, agent → llm-B, ...)
interface ParallelBranchDef {
  name: string;               // toSnakeCase(node.label)
  endpoint_name: string;
  model: string;
  max_iterations: number;     // controls inner ReAct loop for this branch
  tools: ToolDef[];           // ucf nodes → this branch's LLM
  retrievers: RetrieverDef[]; // vs nodes → this branch's LLM
  lakebase_tools: LakebaseDef[]; // lakebase nodes → this branch's LLM
}

// ── BundleConfig ──────────────────────────────────────────────────────────────

export interface BundleConfig {
  project_name: string;
  uc_catalog: string;
  databricks_host: string;
  include_retriever: 'yes' | 'no';
  include_tools: 'yes' | 'no';
  include_agent: 'yes';
  include_evaluation: 'yes' | 'no';
  vector_search_endpoint: string;
  llm_model_name: string;
  llm_max_iterations: number;
  github_runner_group: string;
  // All nodes (for stubs, resource YAMLs, deployment notebooks)
  tools: ToolDef[];
  retrievers: RetrieverDef[];
  // Connectivity-filtered: only nodes with edge → agent
  agent_tools: ToolDef[];
  agent_retrievers: RetrieverDef[];
  agent_lakebase_tools: LakebaseDef[];
  // Multi-LLM topology
  workflow_pattern: 'single' | 'supervisor_worker' | 'sequential' | 'routed';
  supervisor_llm: SupervisorLLMDef | null;
  supervisor_max_iterations: number;
  supervisor_description: string;
  router_description: string;
  worker_llms: WorkerLLMDef[];
  pipeline_stages: PipelineLLMDef[];   // ordered chain for sequential pattern
  parallel_branches: ParallelBranchDef[]; // branches for routed pattern
  has_lakebase: boolean;
  lakebase_instance_name: string;
  // CI/CD
  cicd: CICDConfig;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const makeToolDef = (n: AgentNodeData): ToolDef => {
  const cfg = n.config as UCFunctionConfig;
  return { name: cfg.functionName, catalog: cfg.catalog, schema: cfg.schema, description: cfg.description, deploy: cfg.deploy ?? true };
};

const makeLakebaseDef = (n: AgentNodeData): LakebaseDef => {
  const cfg = n.config as LakebaseConfig;
  return {
    name:          toSnakeCase(n.label) || 'lakebase_tool',
    instance_name: cfg.instanceName,
    description:   cfg.description ?? '',
  };
};

const makeRetrieverDef = (n: AgentNodeData): RetrieverDef => {
  const cfg = n.config as VectorSearchConfig;
  return {
    name:          toSnakeCase(n.label) || 'retriever',
    endpoint_name: cfg.endpointName,
    index_name:    cfg.indexName,
    text_column:   cfg.textColumn,
    columns:       cfg.columns,
    num_results:   cfg.numResults,
  };
};

// ── buildBundleConfig ─────────────────────────────────────────────────────────

export const buildBundleConfig = (
  nodes: AgentNodeData[],
  edges: EdgeData[],
  agentName: string,
  host?: string,
  settings?: ProjectSettings,
): BundleConfig => {
  // Group nodes are excluded from topology analysis; lakebase nodes are included (they connect to LLMs)
  const codeNodes      = nodes.filter(n => n.type !== 'group');
  const lbNodes        = codeNodes.filter(n => n.type === 'lakebase');
  const supervisorNode = codeNodes.find(n => n.type === 'supervisor');
  const routerNode     = codeNodes.find(n => n.type === 'router');
  const orchestratorNode = supervisorNode ?? routerNode; // whichever is present
  const llmNodes  = codeNodes.filter(n => n.type === 'llm');
  const vsNodes   = codeNodes.filter(n => n.type === 'vector_search');
  const ucfNodes  = codeNodes.filter(n => n.type === 'uc_function');

  const firstLLMCfg = llmNodes[0]?.config as LLMConfig | undefined;
  const vsCfg       = vsNodes[0]?.config as VectorSearchConfig | undefined;
  const ucfCfg      = ucfNodes[0]?.config as UCFunctionConfig | undefined;

  // ── All nodes (for stubs / resource YAMLs) ────────────────────────────────
  const tools      = ucfNodes.map(makeToolDef);
  const retrievers = vsNodes.map(makeRetrieverDef);

  // ── Connectivity: which nodes have a direct edge → orchestrator (or primary LLM) ─
  // When there's no orchestrator node, tools connect directly to the primary LLM
  // (single tool-calling agent pattern). Use the first LLM as the target.
  const primaryLLMForTools = orchestratorNode ? undefined : llmNodes[0];
  const connectedToAgent = orchestratorNode
    ? new Set(edges.filter(e => e.target === orchestratorNode.id).map(e => e.source))
    : primaryLLMForTools
      ? new Set(edges.filter(e => e.target === primaryLLMForTools.id).map(e => e.source))
      : new Set<string>();

  const agent_tools           = ucfNodes.filter(n => connectedToAgent.has(n.id)).map(makeToolDef);
  const agent_retrievers      = vsNodes.filter(n => connectedToAgent.has(n.id)).map(makeRetrieverDef);
  const agent_lakebase_tools  = lbNodes.filter(n => connectedToAgent.has(n.id)).map(makeLakebaseDef);

  // ── Multi-LLM topology ────────────────────────────────────────────────────

  // LLM-to-LLM edges drive the sequential pipeline pattern.
  const llmToLLMEdges = edges.filter(
    e => llmNodes.some(n => n.id === e.source) && llmNodes.some(n => n.id === e.target)
  );

  // Edges from orchestrator → each LLM node.
  const agentToLLMEdges = orchestratorNode
    ? edges.filter(e => e.source === orchestratorNode.id && llmNodes.some(n => n.id === e.target))
    : [];

  // Worker LLMs: llm nodes that feed back INTO the supervisor (llm → supervisor).
  // Only relevant when there are no LLM-to-LLM edges and a supervisor is present.
  const workerLLMNodes = supervisorNode && llmToLLMEdges.length === 0
    ? llmNodes.filter(n => edges.some(e => e.source === n.id && e.target === supervisorNode.id))
    : [];

  // Supervisor LLM: the llm node that the supervisor points TO (supervisor → llm).
  const supervisorLLMId = supervisorNode
    ? edges.find(e => e.source === supervisorNode.id && llmNodes.some(n => n.id === e.target))?.target
    : undefined;
  const supervisorLLMNode = supervisorLLMId
    ? llmNodes.find(n => n.id === supervisorLLMId)
    : llmNodes.find(n => !workerLLMNodes.includes(n)); // fallback: first non-worker LLM

  const supervisor_llm: SupervisorLLMDef | null = supervisorLLMNode
    ? (() => {
        const cfg = supervisorLLMNode.config as LLMConfig;
        return { endpoint_name: cfg.endpointName, model: cfg.model, max_iterations: cfg.maxIterations ?? 10 };
      })()
    : null;

  // Each worker gets the ucf/vs nodes connected to it.
  const worker_llms: WorkerLLMDef[] = workerLLMNodes.map(workerNode => {
    const cfg = workerNode.config as LLMConfig;
    const connectedToWorker = new Set(
      edges.filter(e => e.target === workerNode.id).map(e => e.source)
    );
    return {
      name:           toSnakeCase(workerNode.label) || `worker_${workerNode.id}`,
      endpoint_name:  cfg.endpointName,
      model:          cfg.model,
      max_iterations: cfg.maxIterations ?? 10,
      tools:          ucfNodes.filter(n => connectedToWorker.has(n.id)).map(makeToolDef),
      retrievers:     vsNodes.filter(n => connectedToWorker.has(n.id)).map(makeRetrieverDef),
      lakebase_tools: lbNodes.filter(n => connectedToWorker.has(n.id)).map(makeLakebaseDef),
    };
  });

  // ── Sequential pipeline: walk llm→llm edges to build ordered stages ──────
  // Start from the LLM the agent connects to; if none, pick the LLM with no
  // incoming LLM edge (i.e. the head of the chain).
  const llmTargetIds = new Set(llmToLLMEdges.map(e => e.target));
  const chainStart = llmToLLMEdges.length > 0
    ? (supervisorLLMNode ?? llmNodes.find(n => !llmTargetIds.has(n.id)))
    : undefined;

  const pipeline_stages: PipelineLLMDef[] = [];
  if (chainStart) {
    let current: AgentNodeData | undefined = chainStart;
    const visited = new Set<string>();
    while (current && !visited.has(current.id)) {
      visited.add(current.id);
      const cfg = current.config as LLMConfig;
      const connectedToStage = new Set(
        edges.filter(e => e.target === current!.id).map(e => e.source)
      );
      pipeline_stages.push({
        name:           toSnakeCase(current.label) || `stage_${pipeline_stages.length + 1}`,
        endpoint_name:  cfg.endpointName,
        model:          cfg.model,
        max_iterations: cfg.maxIterations ?? 10,
        tools:          ucfNodes.filter(n => connectedToStage.has(n.id)).map(makeToolDef),
        retrievers:     vsNodes.filter(n => connectedToStage.has(n.id)).map(makeRetrieverDef),
        lakebase_tools: lbNodes.filter(n => connectedToStage.has(n.id)).map(makeLakebaseDef),
      });
      const nextEdge = llmToLLMEdges.find(e => e.source === current!.id);
      current = nextEdge ? llmNodes.find(n => n.id === nextEdge.target) : undefined;
    }
  }

  // Routed dispatch: router connects to 2+ LLMs directly (router picks one per request).
  const parallelBranchNodes =
    llmToLLMEdges.length === 0 && workerLLMNodes.length === 0 && routerNode && agentToLLMEdges.length > 1
      ? agentToLLMEdges.map(e => llmNodes.find(n => n.id === e.target)!).filter(Boolean)
      : [];

  const parallel_branches: ParallelBranchDef[] = parallelBranchNodes.map(branchNode => {
    const cfg = branchNode.config as LLMConfig;
    const connectedToBranch = new Set(
      edges.filter(e => e.target === branchNode.id).map(e => e.source)
    );
    return {
      name:           toSnakeCase(branchNode.label) || `branch_${parallelBranchNodes.indexOf(branchNode) + 1}`,
      endpoint_name:  cfg.endpointName,
      model:          cfg.model,
      max_iterations: cfg.maxIterations ?? 10,
      tools:          ucfNodes.filter(n => connectedToBranch.has(n.id)).map(makeToolDef),
      retrievers:     vsNodes.filter(n => connectedToBranch.has(n.id)).map(makeRetrieverDef),
      lakebase_tools: lbNodes.filter(n => connectedToBranch.has(n.id)).map(makeLakebaseDef),
    };
  });

  const workflow_pattern: 'single' | 'supervisor_worker' | 'sequential' | 'routed' =
    pipeline_stages.length > 1    ? 'sequential'
    : worker_llms.length > 0      ? 'supervisor_worker'
    : parallel_branches.length > 1 ? 'routed'
    : 'single';

  const supervisor_max_iterations = supervisorNode
    ? (supervisorNode.config as SupervisorConfig).maxIterations ?? 10
    : 10;

  const supervisor_description = supervisorNode
    ? (supervisorNode.config as SupervisorConfig).description ?? ''
    : '';

  const router_description = routerNode
    ? (routerNode.config as RouterConfig).description ?? ''
    : '';

  return {
    project_name:           toProjectName(agentName),
    uc_catalog:             ucfCfg?.catalog ?? 'main',
    databricks_host:        host ?? 'https://',
    include_retriever:      vsNodes.length  > 0 ? 'yes' : 'no',
    include_tools:          ucfNodes.length > 0 ? 'yes' : 'no',
    include_agent:          'yes',
    include_evaluation:     'yes',
    vector_search_endpoint: vsCfg?.endpointName || 'vs_endpoint',
    llm_model_name:         firstLLMCfg?.endpointName ?? 'databricks-meta-llama-3-3-70b-instruct',
    llm_max_iterations:     firstLLMCfg?.maxIterations ?? 10,
    github_runner_group:    'Default',
    tools,
    retrievers,
    agent_tools,
    agent_retrievers,
    agent_lakebase_tools,
    workflow_pattern,
    supervisor_llm,
    supervisor_max_iterations,
    supervisor_description,
    router_description,
    worker_llms,
    pipeline_stages,
    parallel_branches,
    has_lakebase:           settings?.checkpointEnabled ?? false,
    lakebase_instance_name: settings?.checkpointInstanceName ?? '',
    cicd:                   settings?.cicd ?? DEFAULT_CICD_CONFIG,
  };
};

// ── CI/CD workflow generation ─────────────────────────────────────────────────

export const generateCICDWorkflow = (config: BundleConfig): string => {
  const { cicd } = config;
  if (!cicd.enabled) return '';

  switch (cicd.provider) {
    case 'github_actions':  return generateGitHubActionsWorkflow(config);
    case 'azure_devops':    return generateAzureDevOpsWorkflow(config);
    case 'gitlab_ci':       return generateGitLabCIWorkflow(config);
    default:                return '';
  }
};

const evaluationStep = (config: BundleConfig, indent: string, envLabel: string) => {
  if (!config.cicd.runEvaluationOnDeploy) return '';
  const lines = [
    '',
    `${indent}- name: Run Agent Evaluation (${envLabel})`,
    `${indent}  run: |`,
    `${indent}    databricks bundle run ${config.project_name}_evaluation`,
  ];
  if (config.cicd.promotionGate === 'evaluation_threshold') {
    lines.push(
      `${indent}    # Check evaluation score meets threshold`,
      `${indent}    score=$(databricks bundle run ${config.project_name}_evaluation --output json | jq -r '.score')`,
      `${indent}    if (( $(echo "$score < ${config.cicd.evaluationThreshold / 100}" | bc -l) )); then`,
      `${indent}      echo "Evaluation score $score below threshold ${config.cicd.evaluationThreshold}%"`,
      `${indent}      exit 1`,
      `${indent}    fi`,
    );
  }
  return lines.join('\n');
};

const generateGitHubActionsWorkflow = (config: BundleConfig): string => {
  const { cicd, project_name } = config;
  const needsApproval = cicd.promotionGate === 'manual';

  return `name: Deploy ${project_name}

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  id-token: write

jobs:
  validate:
    name: Validate Bundle
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: databricks/setup-cli@main
      - name: Validate bundle
        run: databricks bundle validate
        env:
          DATABRICKS_HOST: \${{ secrets.STAGING_DATABRICKS_HOST }}
          DATABRICKS_TOKEN: \${{ secrets.STAGING_DATABRICKS_TOKEN }}

  deploy-staging:
    name: Deploy to Staging
    needs: validate
    runs-on: ubuntu-latest
    environment: staging
    steps:
      - uses: actions/checkout@v4
      - uses: databricks/setup-cli@main
      - name: Deploy to staging
        run: databricks bundle deploy --target staging
        env:
          DATABRICKS_HOST: \${{ secrets.STAGING_DATABRICKS_HOST }}
          DATABRICKS_TOKEN: \${{ secrets.STAGING_DATABRICKS_TOKEN }}${evaluationStep(config, '      ', 'Staging')}

  deploy-production:
    name: Deploy to Production
    needs: deploy-staging${needsApproval ? '\n    # Requires manual approval in GitHub environment settings' : ''}
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4
      - uses: databricks/setup-cli@main
      - name: Deploy to production
        run: databricks bundle deploy --target production
        env:
          DATABRICKS_HOST: \${{ secrets.PROD_DATABRICKS_HOST }}
          DATABRICKS_TOKEN: \${{ secrets.PROD_DATABRICKS_TOKEN }}
`;
};

const generateAzureDevOpsWorkflow = (config: BundleConfig): string => {
  const { project_name } = config;

  return `trigger:
  branches:
    include:
      - main

pool:
  vmImage: 'ubuntu-latest'

stages:
  - stage: Validate
    displayName: 'Validate Bundle'
    jobs:
      - job: validate
        steps:
          - task: UsePythonVersion@0
            inputs:
              versionSpec: '3.10'
          - script: |
              curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
              databricks bundle validate
            env:
              DATABRICKS_HOST: $(STAGING_DATABRICKS_HOST)
              DATABRICKS_TOKEN: $(STAGING_DATABRICKS_TOKEN)

  - stage: DeployStaging
    displayName: 'Deploy to Staging'
    dependsOn: Validate
    jobs:
      - deployment: staging
        environment: staging
        strategy:
          runOnce:
            deploy:
              steps:
                - script: |
                    curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
                    databricks bundle deploy --target staging
                  env:
                    DATABRICKS_HOST: $(STAGING_DATABRICKS_HOST)
                    DATABRICKS_TOKEN: $(STAGING_DATABRICKS_TOKEN)

  - stage: DeployProduction
    displayName: 'Deploy to Production'
    dependsOn: DeployStaging
    jobs:
      - deployment: production
        environment: production
        strategy:
          runOnce:
            deploy:
              steps:
                - script: |
                    curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
                    databricks bundle deploy --target production
                  env:
                    DATABRICKS_HOST: $(PROD_DATABRICKS_HOST)
                    DATABRICKS_TOKEN: $(PROD_DATABRICKS_TOKEN)
`;
};

const generateGitLabCIWorkflow = (config: BundleConfig): string => {
  const { cicd } = config;

  return `stages:
  - validate
  - deploy-staging
  - deploy-production

variables:
  DATABRICKS_CLI_VERSION: "latest"

.setup-cli: &setup-cli
  before_script:
    - curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh

validate:
  stage: validate
  <<: *setup-cli
  script:
    - databricks bundle validate
  variables:
    DATABRICKS_HOST: $STAGING_DATABRICKS_HOST
    DATABRICKS_TOKEN: $STAGING_DATABRICKS_TOKEN

deploy-staging:
  stage: deploy-staging
  <<: *setup-cli
  script:
    - databricks bundle deploy --target staging
  variables:
    DATABRICKS_HOST: $STAGING_DATABRICKS_HOST
    DATABRICKS_TOKEN: $STAGING_DATABRICKS_TOKEN
  environment:
    name: staging

deploy-production:
  stage: deploy-production
  <<: *setup-cli
  script:
    - databricks bundle deploy --target production
  variables:
    DATABRICKS_HOST: $PROD_DATABRICKS_HOST
    DATABRICKS_TOKEN: $PROD_DATABRICKS_TOKEN
  environment:
    name: production
  when: ${cicd.promotionGate === 'manual' ? 'manual' : 'on_success'}
`;
};

// ── ZIP download ──────────────────────────────────────────────────────────────

export const downloadProjectZip = async (
  nodes: AgentNodeData[],
  edges: EdgeData[],
  agentName: string,
  host?: string,
  settings?: ProjectSettings,
): Promise<void> => {
  const config = buildBundleConfig(nodes, edges, agentName, host, settings);

  const response = await fetch(
    `${import.meta.env.VITE_API_URL}/generate`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }
  );

  const blob = await response.blob();
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = `${config.project_name}.zip`;
  a.click();
  URL.revokeObjectURL(url);
};
