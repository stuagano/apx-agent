/**
 * SequentialAgent — runs agents in order, each receiving the previous output as context.
 *
 * @example
 * const pipeline = new SequentialAgent([
 *   analyzerAgent,   // Step 1: analyze the data
 *   plannerAgent,    // Step 2: plan based on analysis
 *   executorAgent,   // Step 3: execute the plan
 * ]);
 *
 * const result = await pipeline.run([{ role: 'user', content: 'Investigate table X' }]);
 */

import { AgentState } from './state.js';
import type { Message, Runnable } from './types.js';

export class SequentialAgent implements Runnable {
  private agents: Runnable[];
  private instructions: string;

  constructor(agents: Runnable[], instructions: string = '') {
    if (agents.length === 0) {
      throw new Error('SequentialAgent requires at least one agent');
    }
    this.agents = agents;
    this.instructions = instructions;
  }

  async run(messages: Message[], state?: AgentState): Promise<string> {
    const agentState = state ?? new AgentState();
    let context = this.prependInstructions(messages, agentState);
    let result = '';

    for (const agent of this.agents) {
      // Clear turn-scoped temp values before each step
      agentState.clearTemp();

      result = await agent.run(context, agentState);

      // If the agent has an outputKey, store its result in state
      if (agent.outputKey) {
        agentState.set(agent.outputKey, result);
      }

      context = [...context, { role: 'assistant', content: result }];
    }

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
