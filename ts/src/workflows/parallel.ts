/**
 * ParallelAgent — runs all agents concurrently with the same input, merges responses.
 *
 * @example
 * const gatherer = new ParallelAgent([
 *   weatherFetcher,  // Fetch weather data
 *   newsFetcher,     // Fetch news data
 * ]);
 *
 * const combined = await gatherer.run([{ role: 'user', content: 'Brief me on today' }]);
 * // Returns: weather response + "\n\n" + news response
 */

import type { Message, Runnable } from './types.js';

export class ParallelAgent implements Runnable {
  private agents: Runnable[];
  private instructions: string;
  private separator: string;

  constructor(
    agents: Runnable[],
    options: { instructions?: string; separator?: string } = {},
  ) {
    if (agents.length === 0) {
      throw new Error('ParallelAgent requires at least one agent');
    }
    this.agents = agents;
    this.instructions = options.instructions ?? '';
    this.separator = options.separator ?? '\n\n';
  }

  async run(messages: Message[]): Promise<string> {
    const context = this.prependInstructions(messages);
    const results = await Promise.all(
      this.agents.map((agent) => agent.run(context)),
    );
    return results.join(this.separator);
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    // Run all to completion (streaming parallel is complex), yield combined
    yield await this.run(messages);
  }

  collectTools() {
    return this.agents.flatMap((a) => a.collectTools?.() ?? []);
  }

  private prependInstructions(messages: Message[]): Message[] {
    if (!this.instructions) return [...messages];
    return [{ role: 'system', content: this.instructions }, ...messages];
  }
}
