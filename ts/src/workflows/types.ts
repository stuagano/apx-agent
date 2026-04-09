/**
 * Shared types for workflow agents.
 *
 * A workflow agent composes sub-agents and controls execution flow.
 * Sub-agents don't know they're being composed — they just receive
 * messages and return text.
 */

import type { AgentTool } from '../agent/tools.js';
import type { AgentState } from './state.js';

/** A message in the conversation. */
export interface Message {
  role: string;
  content: string;
}

/**
 * Base interface for any agent that can be composed in a workflow.
 *
 * This is intentionally minimal — a plain function that takes messages
 * and returns text. Workflow agents (Sequential, Parallel, Loop, Router,
 * Handoff) all implement this interface while adding composition logic.
 */
export interface Runnable {
  /**
   * When set, the agent's output is stored in AgentState under this key
   * after execution. Follows the Google ADK output_key pattern.
   */
  outputKey?: string;

  /** Run the agent and return the final text. */
  run(messages: Message[], state?: AgentState): Promise<string>;

  /** Stream text chunks. Default: run to completion and yield once. */
  stream?(messages: Message[], state?: AgentState): AsyncGenerator<string>;

  /** Collect tool descriptors for all agents in the tree. */
  collectTools?(): AgentTool[];
}
