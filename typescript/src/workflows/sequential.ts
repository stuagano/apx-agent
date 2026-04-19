/**
 * SequentialAgent — runs agents in order, each receiving the previous output as context.
 *
 * Optionally durable: pass a `WorkflowEngine` to persist each agent's
 * output so a crashed or restarted pipeline resumes at the first
 * uncompleted step.
 *
 * @example
 * const pipeline = new SequentialAgent([
 *   analyzerAgent,   // Step 1: analyze the data
 *   plannerAgent,    // Step 2: plan based on analysis
 *   executorAgent,   // Step 3: execute the plan
 * ]);
 *
 * const result = await pipeline.run([{ role: 'user', content: 'Investigate table X' }]);
 *
 * @example Durable
 * const engine = new DeltaEngine({ ... });
 * const pipeline = new SequentialAgent(
 *   [analyzerAgent, plannerAgent, executorAgent],
 *   'Investigate missing data.',
 *   { engine, runId: 'investigation-42' },
 * );
 */

import type { WorkflowEngine } from './engine.js';
import { InMemoryEngine } from './engine-memory.js';
import { AgentState } from './state.js';
import type { Message, Runnable } from './types.js';

export interface SequentialAgentOptions {
  /** Durable execution engine. Default: fresh in-process `InMemoryEngine`. */
  engine?: WorkflowEngine;
  /** If set, resume an existing run with this ID. */
  runId?: string;
  /** Workflow name for engine run records. Default: `sequential`. */
  workflowName?: string;
}

export class SequentialAgent implements Runnable {
  private agents: Runnable[];
  private instructions: string;
  private engine: WorkflowEngine;
  private providedRunId: string | undefined;
  private workflowName: string;

  constructor(
    agents: Runnable[],
    instructions: string = '',
    options: SequentialAgentOptions = {},
  ) {
    if (agents.length === 0) {
      throw new Error('SequentialAgent requires at least one agent');
    }
    this.agents = agents;
    this.instructions = instructions;
    this.engine = options.engine ?? new InMemoryEngine();
    this.providedRunId = options.runId;
    this.workflowName = options.workflowName ?? 'sequential';
  }

  async run(messages: Message[], state?: AgentState): Promise<string> {
    const agentState = state ?? new AgentState();
    const runId = await this.engine.startRun(
      this.workflowName,
      { messages, instructions: this.instructions },
      { runId: this.providedRunId },
    );

    let context = this.prependInstructions(messages, agentState);
    let result = '';

    for (let i = 0; i < this.agents.length; i++) {
      const agent = this.agents[i];

      // Clear turn-scoped temp values before each step
      agentState.clearTemp();

      // engine.step replays the cached output on resume, otherwise invokes
      // the handler and persists the result.
      result = await this.engine.step<string>(runId, `step-${i}`, () =>
        agent.run(context, agentState),
      );

      if (agent.outputKey) {
        agentState.set(agent.outputKey, result);
      }

      context = [...context, { role: 'assistant', content: result }];
    }

    await this.engine.finishRun(runId, 'completed', result);
    return result;
  }

  async *stream(messages: Message[], state?: AgentState): AsyncGenerator<string> {
    const agentState = state ?? new AgentState();
    let context = this.prependInstructions(messages, agentState);

    // Run all but the last agent to completion
    for (const agent of this.agents.slice(0, -1)) {
      agentState.clearTemp();

      const result = await agent.run(context, agentState);

      if (agent.outputKey) {
        agentState.set(agent.outputKey, result);
      }

      context = [...context, { role: 'assistant', content: result }];
    }

    // Stream the last agent
    const last = this.agents[this.agents.length - 1];
    agentState.clearTemp();

    let lastResult: string;
    if (last.stream) {
      const chunks: string[] = [];
      for await (const chunk of last.stream(context, agentState)) {
        chunks.push(chunk);
        yield chunk;
      }
      lastResult = chunks.join('');
    } else {
      lastResult = await last.run(context, agentState);
      yield lastResult;
    }

    // Store output for the last agent too
    if (last.outputKey) {
      agentState.set(last.outputKey, lastResult);
    }
  }

  collectTools() {
    return this.agents.flatMap((a) => a.collectTools?.() ?? []);
  }

  /**
   * Prepend system instructions to messages.
   * If state is provided, interpolate {variables} in the instructions.
   */
  private prependInstructions(messages: Message[], agentState?: AgentState): Message[] {
    if (!this.instructions) return [...messages];

    const resolved = agentState
      ? agentState.interpolate(this.instructions)
      : this.instructions;

    return [{ role: 'system', content: resolved }, ...messages];
  }
}
