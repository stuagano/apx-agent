/**
 * LoopAgent — runs a sub-agent repeatedly until a stop condition is met.
 *
 * The stop condition can be:
 * - The sub-agent's response matches a predicate (`stopWhen`)
 * - `maxIterations` is reached
 *
 * Each iteration receives the previous response appended to the conversation.
 *
 * Optionally durable: pass a `WorkflowEngine` to persist each iteration's
 * output so a crashed or restarted run resumes from the last completed
 * iteration instead of replaying work.
 *
 * @example
 * const refiner = new LoopAgent(writerAgent, {
 *   maxIterations: 3,
 *   stopWhen: (result) => result.includes('FINAL'),
 * });
 *
 * @example Durable
 * const engine = new DeltaEngine({ ... });
 * const refiner = new LoopAgent(writerAgent, { engine, runId: 'draft-42' });
 */

import type { WorkflowEngine } from './engine.js';
import { InMemoryEngine } from './engine-memory.js';
import type { Message, Runnable } from './types.js';

export type StopPredicate = (result: string, iteration: number) => boolean;

export interface LoopAgentOptions {
  maxIterations?: number;
  stopWhen?: StopPredicate;
  /**
   * Durable execution engine. If omitted, an in-process `InMemoryEngine` is
   * used — preserves the pre-durable behavior (state lost on restart).
   */
  engine?: WorkflowEngine;
  /** If set, resume an existing run with this ID. */
  runId?: string;
  /** Workflow name for engine run records. Default: `loop`. */
  workflowName?: string;
}

export class LoopAgent implements Runnable {
  private agent: Runnable;
  private maxIterations: number;
  private stopWhen: StopPredicate | null;
  private engine: WorkflowEngine;
  private providedRunId: string | undefined;
  private workflowName: string;

  constructor(agent: Runnable, options: LoopAgentOptions = {}) {
    this.agent = agent;
    this.maxIterations = options.maxIterations ?? 5;
    this.stopWhen = options.stopWhen ?? null;
    this.engine = options.engine ?? new InMemoryEngine();
    this.providedRunId = options.runId;
    this.workflowName = options.workflowName ?? 'loop';
  }

  async run(messages: Message[]): Promise<string> {
    const runId = await this.engine.startRun(
      this.workflowName,
      { messages, maxIterations: this.maxIterations },
      { runId: this.providedRunId },
    );

    // Rebuild context from any iterations that were already persisted
    // (non-empty only on resume).
    const snapshot = await this.engine.getRun(runId);
    const completed = (snapshot?.steps ?? [])
      .filter((s) => s.stepKey.startsWith('iter-') && s.status === 'completed')
      .map((s) => ({
        iter: Number.parseInt(s.stepKey.slice('iter-'.length), 10),
        result: s.output as string,
      }))
      .sort((a, b) => a.iter - b.iter);

    let context = [...messages];
    let result = '';
    let nextIter = 0;

    for (const { iter, result: iterResult } of completed) {
      result = iterResult;
      // Replay the stop predicate against the cached result — if it would
      // have stopped the original run here, stop now too.
      if (this.stopWhen?.(result, iter)) {
        await this.engine.finishRun(runId, 'completed', result);
        return result;
      }
      context = [...context, { role: 'assistant', content: result }];
      nextIter = iter + 1;
    }

    for (let i = nextIter; i < this.maxIterations; i++) {
      result = await this.engine.step<string>(runId, `iter-${i}`, () =>
        this.agent.run(context),
      );

      if (this.stopWhen?.(result, i)) {
        break;
      }

      context = [...context, { role: 'assistant', content: result }];
    }

    await this.engine.finishRun(runId, 'completed', result);
    return result;
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    yield await this.run(messages);
  }

  collectTools() {
    return this.agent.collectTools?.() ?? [];
  }
}
