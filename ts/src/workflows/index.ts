/**
 * Workflow agents — deterministic composition patterns for multi-agent systems.
 *
 * These are the ADK-equivalent abstractions for Databricks:
 * - SequentialAgent: pipeline execution (analyze → plan → execute)
 * - ParallelAgent: fan-out/gather (fetch weather + news concurrently)
 * - LoopAgent: iterative refinement (draft → review → revise until done)
 * - RouterAgent: conditional routing (billing → bill agent, data → triage agent)
 * - HandoffAgent: peer handoff (triage → billing mid-conversation)
 */

export { SequentialAgent } from './sequential.js';
export { ParallelAgent } from './parallel.js';
export { LoopAgent } from './loop.js';
export type { StopPredicate } from './loop.js';
export { RouterAgent } from './router.js';
export type { Route, RouterConfig } from './router.js';
export { HandoffAgent } from './handoff.js';
export type { HandoffConfig } from './handoff.js';
export { RemoteAgent } from './remote.js';
export type { RemoteAgentConfig } from './remote.js';
export { AgentState } from './state.js';
export {
  Session,
  InMemorySessionStore,
  setDefaultSessionStore,
  getDefaultSessionStore,
} from './session.js';
export type { SessionStore, SessionSnapshot } from './session.js';
export type { Message, Runnable } from './types.js';

// Evolutionary workflow — population management across generations
export { EvolutionaryAgent } from './evolutionary.js';
export type { EvolutionaryConfig, EvolutionState, GenerationResult } from './evolutionary.js';
export { PopulationStore } from './population.js';
export type { PopulationStoreConfig } from './population.js';
export { paretoDominates, paretoFrontier, selectSurvivors } from './pareto.js';
export { createHypothesis, compositeFitness } from './hypothesis.js';
export type { Hypothesis } from './hypothesis.js';
