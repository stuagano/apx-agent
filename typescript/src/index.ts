/**
 * apx-internal-runtime — internal APX runtime for generated Databricks hosts.
 *
 * This package is not a public authoring surface. Python APX declarations stay
 * external; generated Apps/serving hosts consume this runtime internally.
 *
 * @example
 * import { createApp, server, genie } from '@databricks/appkit';
 * import { createAgentPlugin, createDiscoveryPlugin, createMcpPlugin, createDevPlugin, defineTool } from 'apx-internal-runtime';
 * import { z } from 'zod';
 *
 * const getLineage = defineTool({
 *   name: 'get_table_lineage',
 *   description: 'Get upstream sources for a table',
 *   parameters: z.object({ tableName: z.string() }),
 *   handler: async ({ tableName }) => { ... },
 * });
 *
 * const agentPlugin = createAgentPlugin({
 *   model: 'databricks-claude-sonnet-4-6',
 *   instructions: 'You investigate missing data.',
 *   tools: [getLineage],
 * });
 *
 * const agentExports = () => agentPlugin.exports();
 *
 * createApp({
 *   plugins: [
 *     server(),
 *     genie(),
 *     agentPlugin,
 *     createDiscoveryPlugin({ registry: '$AGENT_HUB_URL' }, agentExports),
 *     createMcpPlugin({}, agentExports),
 *     createDevPlugin({}, agentExports),
 *   ],
 * });
 */

// Agent plugin — tool registration, agent loop, /responses
export { createAgentPlugin, defineTool, zodToJsonSchema, toStrictSchema, toolsToFunctionSchemas } from './agent/index.js';
export type { AgentConfig, AgentExports, AgentTool, FunctionSchema } from './agent/index.js';
export { runViaSDK, streamViaSDK, initDatabricksClient, toFunctionTool, callSubAgent, agentTool } from './agent/index.js';
export type { AgentToolOptions } from './agent/index.js';
export { getRequestContext, runWithContext, withAutonomousTrace } from './agent/request-context.js';
export type { RequestContext } from './agent/request-context.js';

// Discovery plugin — A2A card, registry auto-registration
export { createDiscoveryPlugin } from './discovery/index.js';
export type { DiscoveryConfig, AgentCard } from './discovery/index.js';

// MCP plugin — MCP server exposure for Supervisor / Claude Desktop / Cursor
export { createMcpPlugin, mcpAuthStore, getMcpAuth } from './mcp/index.js';
export type { McpConfig, McpAuthContext } from './mcp/index.js';

// Dev UI plugin — chat testing + tool inspector
export { createDevPlugin } from './dev/index.js';
export type { DevUIConfig } from './dev/index.js';

// Workflow agents — deterministic composition patterns
export {
  SequentialAgent,
  ParallelAgent,
  LoopAgent,
  RouterAgent,
  HandoffAgent,
  RemoteAgent,
  AgentState,
  Session,
  InMemorySessionStore,
  setDefaultSessionStore,
  getDefaultSessionStore,
} from './workflows/index.js';
export type {
  Message,
  Runnable,
  StopPredicate,
  Route,
  RouterConfig,
  HandoffConfig,
  RemoteAgentConfig,
  SessionStore,
  SessionSnapshot,
} from './workflows/index.js';

// Eval bridge — predict function + harness for /responses endpoints
export { createPredictFn, runEval } from './eval/index.js';
export type {
  PredictFn,
  PredictOptions,
  PredictInput,
  EvalCase,
  EvalResult,
  RunEvalOptions,
  EvalSummary,
} from './eval/index.js';

// Genie tool factories
export { genieTool, genieQueryTool } from './genie.js';
export type { GenieToolOptions, GenieQueryToolOptions, GenieQueryResult } from './genie.js';

// Unity Catalog tool factories
export { catalogTool, lineageTool, schemaTool, ucFunctionTool } from './catalog.js';
export type { CatalogToolOptions, UcFunctionToolOptions } from './catalog.js';

// Connectors — domain tool factories for Lakebase, Vector Search, Doc Parser
export {
  parseEntitySchema,
  resolveHost,
  resolveToken,
  buildSqlParams,
  dbFetch,
  createLakebaseQueryTool,
  createLakebaseMutateTool,
  createLakebaseSchemaInspectTool,
  createVSQueryTool,
  createVSUpsertTool,
  createVSDeleteTool,
  createDocUploadTool,
  createDocChunkTool,
  createDocExtractEntitiesTool,
  chunkText,
} from './connectors/index.js';
export type {
  ConnectorConfig,
  EntitySchema,
  EntityDef,
  EdgeDef,
  FieldDef,
  ExtractionConfig,
  FitnessConfig,
  EvolutionConfig,
} from './connectors/index.js';

// Evolutionary workflow
export {
  EvolutionaryAgent,
  PopulationStore,
  paretoDominates,
  paretoFrontier,
  selectSurvivors,
  createHypothesis,
  compositeFitness,
} from './workflows/index.js';
export type {
  Hypothesis,
  EvolutionaryConfig,
  EvolutionState,
  GenerationResult,
  PopulationStoreConfig,
} from './workflows/index.js';

// Durable execution — WorkflowEngine interface + backends
export {
  InMemoryEngine,
  DeltaEngine,
  InngestEngine,
  StepFailedError,
} from './workflows/index.js';
export type {
  WorkflowEngine,
  DeltaEngineConfig,
  InngestStep,
  RunStatus,
  RunSnapshot,
  RunSummary,
  RunFilter,
  StepRecord,
} from './workflows/index.js';

// Trace system
export {
  createTrace,
  addSpan,
  endSpan,
  endTrace,
  getTraces,
  getTrace,
  storeTrace,
  truncate,
} from './trace.js';
export type { Trace, TraceSpan, TraceContext } from './trace.js';

// Audit log schema — apx.* span attributes
export {
  AuditAttrs,
  setAuditAttrs,
  hashForAudit,
  outputSummary,
  inputKeysSummary,
} from './audit.js';

// Local lightweight guards — zero-latency runtime checks
export {
  PermissionError,
  RateLimit,
  ToolAllowlist,
  ToolDenylist,
  promptInjectionHeuristic,
  compose,
} from './guards.js';

// Cost tracking — DBU / $ per agent or serving endpoint
export { costForAgent, costForEndpoint } from './cost.js';
export type { CostBreakdown, CostRow, SqlExecutor } from './cost.js';

// Canary / A-B deployment helpers
export {
  CanaryReport,
  VersionMetrics,
  getCanaryConfig,
  deployCanary,
  promoteCanary,
  rollbackCanary,
  analyzeCanary,
} from './canary.js';
export type {
  CanaryConfig,
  ServingEndpointsClient,
  TraceSearch,
  EndpointInfo,
  EndpointConfigResponse,
} from './canary.js';

// Databricks Managed MCP integration
export {
  managedMcpUrls,
  managedMcpClientConfig,
} from './managed-mcp.js';
export type {
  ManagedMCPEndpoint,
  ResourceSpec,
  ResourceKind,
} from './managed-mcp.js';

// Topology visualization
export {
  discoverTopology,
  renderTopology,
} from './topology.js';
export type {
  AgentNode,
  TopologyEdge,
  Topology,
  RegisteredModelsClient,
  RegisteredModel,
} from './topology.js';

// Resource declaration — auto-derive Mosaic AI resources from an agent tree
export {
  makeResourceSpec,
  attachResources,
  getResources,
  subAgentToEndpoint,
  collectResourceSpecs,
  mlflowResourcesFor,
} from './resources.js';
export type { CollectResourceSpecsOptions, MlflowResource } from './resources.js';

// Trace exporter — MLflow traces → Delta table
export {
  _normaliseTrace,
  escapeSql as escapeSqlForTraceExport,
  exportTraces,
} from './trace-export.js';
export type {
  TraceAttrs,
  RawTrace,
  TraceLike,
  TraceInfoLike,
  TraceDataLike,
  TraceSearch as TraceExportSearch,
  SqlExecutor as TraceExportSqlExecutor,
  NormalisedTrace,
  ExportResult,
  ExportTracesOptions,
} from './trace-export.js';

// Batch / scheduled-job invocation
export { runOnce } from './run-once.js';
export type {
  RunOnceOptions,
  RunViaSDKFn,
  StreamViaSDKFn,
  RunViaSDKParams,
} from './run-once.js';

// Hot-swap the LLM endpoint on a deployed agent without re-logging
export {
  APX_MODEL_OVERRIDE_ENV,
  hotSwapModel,
  getActiveOverride,
} from './hot-swap.js';
export type { HotSwapResult } from './hot-swap.js';

// Static lint
export { Severity, lintAgent } from './lint.js';
export type {
  LintFinding,
  LintableAgent,
  LintableTool,
  LintAgentOptions,
} from './lint.js';

// @tool decorator equivalent — defineUcTool factory + UC publish
export { defineUcTool, getToolMetadata } from './tool.js';
export type { ToolMetadata, DefineUcToolOptions } from './tool.js';
export { publishToolsToUc } from './tool-publish.js';
export type {
  UcFunctionClient,
  WsClient,
  PublishResult,
  PublishToolsToUcOptions,
} from './tool-publish.js';

// Mosaic AI Supervisor Agent publishing
export {
  _slug as supervisorToolSlug,
  createSupervisorAgent,
  publishToSupervisor,
} from './publish.js';
export type {
  SupervisorAgent,
  SupervisorTool,
  SupervisorAgentsClient,
} from './publish.js';

// Watchdog — full compliance posture integration
export {
  makeWatchdogDecision,
  noOpTransport,
  WatchdogClient,
  WatchdogGuard,
  emitAgentMetadata,
  setUcTagsForAgent,
  makeUcViolationWriter,
  makeMcpTransport,
  makeWatchdogTransport,
} from './watchdog.js';
export type {
  WatchdogDecision,
  TransportFn,
  WatchdogClientOptions,
  EvaluateOpts as WatchdogEvaluateOpts,
  WatchdogGuardOptions,
  InputGuard,
  OutputGuard,
  BeforeTool,
  BeforeModel,
  ToolDescriptor,
  EmitAgentMetadataOptions,
  AgentMetadata,
  MlflowTagClient,
  SetUcTagsOptions,
  SqlExecutor as WatchdogSqlExecutor,
  MakeUcViolationWriterOptions,
  McpToolCallFn,
  MakeMcpTransportOptions,
  MakeWatchdogTransportOptions,
} from './watchdog.js';

// Durable session stores — Delta + Lakebase (Postgres)
export { DeltaSessionStore } from './session-delta.js';
export type {
  SqlQueryExecutor as DeltaQueryExecutor,
  SqlExecExecutor as DeltaExecExecutor,
  DeltaSessionStoreOptions,
} from './session-delta.js';
export { LakebaseSessionStore } from './session-lakebase.js';
export type {
  PgQueryExecutor,
  PgExecExecutor,
  LakebaseSessionStoreOptions,
} from './session-lakebase.js';

// Mosaic AI Agent Evaluation wrapper
export { evaluate, extractMessages, extractResponseText } from './evaluate.js';
export type {
  EvalMessage,
  MlflowEvaluateArgs,
  MlflowEvaluateFn,
  MlflowSetExperimentFn,
  EvalOptions,
} from './evaluate.js';

// Chain evaluation — eval + MLflow trace correlation
export {
  evaluateChain,
  requestTextsFromEvalset,
  buildCaseFromTrace,
} from './evaluate-chain.js';
export type {
  ChainCaseResult,
  ChainEvalReport,
  TraceSearchFn,
  EvaluateChainOptions,
} from './evaluate-chain.js';

// ChatAgent compile + log (Mosaic AI deploy path) — injectable mlflowLogModel
export { compileToChatAgent, logAgent } from './chat-agent.js';
export type {
  ChatAgentMessage,
  ChatAgentResponse,
  ChatAgentChunk,
  ChatContext,
  CompiledChatAgent,
  CompileToChatAgentOptions,
  LogAgentResult,
  LogAgentOptions,
  MlflowLogModelArgs,
  MlflowLogModelFn,
  PredictInput as ChatAgentPredictInput,
} from './chat-agent.js';

// ResponsesAgent compile (Databricks Apps deploy path)
export {
  compileToResponsesAgent,
  mountResponsesAgent,
} from './responses-agent.js';
export type {
  CompileToResponsesAgentOptions,
  CompiledResponsesAgent,
  ResponsesAgentRequest,
  ResponsesAgentResponse,
  ResponsesAgentStreamEvent,
  ResponsesAgentInputItem,
  ResponsesAgentOutputItem,
  MountResponsesAgentOptions,
} from './responses-agent.js';

// Unified OBO header extraction — single source of truth across both runtimes
export { extractOboHeaders } from './obo.js';
export type {
  NormalizedObo,
  ExtractOboHeadersOptions,
  HeadersInput,
} from './obo.js';

// MLflow tracing helpers — safe no-op when MLflow JS unavailable
export {
  safeSpan,
  withSafeSpan,
  setSpanOutputs,
  setSpanAttribute as setMlflowSpanAttribute,
  currentActiveSpan,
  setMlflowStartSpan,
  setActiveSpanResolver,
  setMlflowAvailabilityCheck,
  isMlflowAvailable,
  enableLangchainAutolog,
  autologIfEnv,
} from './mlflow-tracing.js';
export type {
  SpanLike as MlflowSpanLike,
  SafeSpanOptions,
  MlflowStartSpan,
  ActiveSpanResolver,
  MlflowAvailabilityCheck,
} from './mlflow-tracing.js';

// /invocations route handler — Express bridge for Mosaic AI ChatAgent
export { mountInvocationsRoute } from './invocations.js';
export type {
  MountInvocationsOptions,
  ChatAgentLike,
  ChatAgentPredictOptions,
} from './invocations.js';

// MemoryStore + ExampleStore foundations — types, helpers, in-memory baselines
export {
  InMemoryMemoryStore,
  cosineSimilarity,
  tagsOverlap,
  materializeMemory,
  applyPatch as applyMemoryPatch,
  newMemoryId,
} from './memory.js';
export type {
  Memory,
  MemoryInput,
  MemoryFilter,
  MemoryPatch,
  MemoryStore,
  RecallOptions,
  RecallResult,
  EmbeddingFn,
} from './memory.js';

export {
  InMemoryExampleStore,
  materializeExample,
  applyExamplePatch,
  newExampleId,
} from './example.js';
export type {
  Example,
  ExampleInput,
  ExampleFilter,
  ExamplePatch,
  ExampleStore,
  FindSimilarOptions,
  ExampleResult,
} from './example.js';

// Lakebase pgvector backends
export {
  LakebaseMemoryStore,
  vectorLiteral,
  tagsLiteral,
} from './memory-lakebase.js';
export type {
  LakebaseMemoryStoreOptions,
  PgQueryExecutor as LakebaseMemoryQueryExecutor,
  PgExecExecutor as LakebaseMemoryExecExecutor,
} from './memory-lakebase.js';
export { LakebaseExampleStore } from './example-lakebase.js';
export type { LakebaseExampleStoreOptions } from './example-lakebase.js';

// Delta backends (with optional Databricks Vector Search delegation)
export { DeltaMemoryStore } from './memory-delta.js';
export type {
  DeltaMemoryStoreOptions,
  VectorSearchClient,
  SqlQueryExecutor as DeltaMemoryQueryExecutor,
  SqlExecExecutor as DeltaMemoryExecExecutor,
} from './memory-delta.js';
export { DeltaExampleStore } from './example-delta.js';
export type { DeltaExampleStoreOptions } from './example-delta.js';

// Agent-side integration: tools + prompt assembly
export { makeMemoryTools } from './memory-tools.js';
export type { MakeMemoryToolsOptions, MemoryToolName } from './memory-tools.js';
export { makeExampleTools } from './example-tools.js';
export type { MakeExampleToolsOptions, ExampleToolName } from './example-tools.js';
export {
  assembleMemoryContext,
  assembleExampleContext,
  assembleContext,
} from './prompt-assembly.js';
export type { AssembleContextOptions } from './prompt-assembly.js';

// Service Policies — shared declarations, validation, and local decision ordering
export {
  validateServicePolicies,
  orderedPolicies,
  evaluateOrderedServicePolicies,
} from './service-policies.js';
export type {
  ServicePolicyTargetType,
  ServicePolicyKind,
  BuiltinServicePolicy,
  ServicePolicyPhase,
  ServicePolicyMode,
  LocalPolicyMode,
  NativePolicyMode,
  ServicePolicyAction,
  ServicePolicy,
  ServicePolicyAttachment,
  AbacSelector,
  ServicePoliciesConfig,
  ServicePolicyEvent,
  ServicePolicyDecision,
  ServicePolicyEvaluationResult,
  ServicePolicyEvaluator,
} from './service-policies.js';

// Example mining — extract few-shot examples from session history
export { mineExamples, pairTurns } from './example-mining.js';
export type {
  Turn as ExampleMiningTurn,
  MineResult,
  MineExamplesOptions,
} from './example-mining.js';

// Memory consolidation — LLM-summarize older memories into a single rollup
export { consolidateMemories } from './memory-consolidate.js';
export type {
  ConsolidateResult,
  ConsolidateMemoriesOptions,
  SummarizeFn,
} from './memory-consolidate.js';
