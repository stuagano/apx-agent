/**
 * LoopAgent — runs a sub-agent repeatedly until a stop condition is met.
 *
 * The stop condition can be:
 * - The sub-agent's response matches a predicate (`stopWhen`)
 * - `maxIterations` is reached
 *
 * Each iteration receives the previous response appended to the conversation.
 *
 * @example
 * const refiner = new LoopAgent(writerAgent, {
 *   maxIterations: 3,
 *   stopWhen: (result) => result.includes('FINAL'),
 * });
 */

import type { Message, Runnable } from './types.js';

export type StopPredicate = (result: string, iteration: number) => boolean;

export class LoopAgent implements Runnable {
  private agent: Runnable;
  private maxIterations: number;
  private stopWhen: StopPredicate | null;

  constructor(
    agent: Runnable,
    options: {
      maxIterations?: number;
      stopWhen?: StopPredicate;
    } = {},
  ) {
    this.agent = agent;
    this.maxIterations = options.maxIterations ?? 5;
    this.stopWhen = options.stopWhen ?? null;
  }

  async run(messages: Message[]): Promise<string> {
    let context = [...messages];
    let result = '';

    for (let i = 0; i < this.maxIterations; i++) {
      result = await this.agent.run(context);

      if (this.stopWhen?.(result, i)) {
        break;
      }

      context = [...context, { role: 'assistant', content: result }];
    }

    return result;
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    yield await this.run(messages);
  }

  collectTools() {
    return this.agent.collectTools?.() ?? [];
  }
}
