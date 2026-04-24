/**
 * appkit-agent — AppKit plugins for building AI agents on Databricks Apps.
 *
 * @example
 * import { createApp, server, genie } from '@databricks/appkit';
 * import { createAgentPlugin, createDiscoveryPlugin, createMcpPlugin, createDevPlugin, defineTool } from 'appkit-agent';
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
export { runViaSDK, streamViaSDK, initDatabricksClient, toFunctionTool, toSubAgentTool } from './agent/index.js';
export { getRequestContext, runWithContext } from './agent/request-context.js';
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

// Genie tool factory
export { genieTool } from './genie.js';
export type { GenieToolOptions } from './genie.js';

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
