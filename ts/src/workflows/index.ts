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
export type { Message, Runnable } from './types.js';
