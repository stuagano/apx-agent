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

  async run(messages: Message[]): Promise<string> {
    let context = this.prependInstructions(messages);
    let result = '';

    for (const agent of this.agents) {
      result = await agent.run(context);
      context = [...context, { role: 'assistant', content: result }];
    }

    return result;
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    let context = this.prependInstructions(messages);

    // Run all but the last agent to completion
    for (const agent of this.agents.slice(0, -1)) {
      const result = await agent.run(context);
      context = [...context, { role: 'assistant', content: result }];
    }

    // Stream the last agent
    const last = this.agents[this.agents.length - 1];
    if (last.stream) {
      yield* last.stream(context);
    } else {
      yield await last.run(context);
    }
  }

  collectTools() {
    return this.agents.flatMap((a) => a.collectTools?.() ?? []);
  }

  private prependInstructions(messages: Message[]): Message[] {
    if (!this.instructions) return [...messages];
    return [{ role: 'system', content: this.instructions }, ...messages];
  }
}
