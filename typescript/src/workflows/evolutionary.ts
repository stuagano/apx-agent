/**
 * EvolutionaryAgent — runs a background evolutionary loop over a PopulationStore.
 *
 * Each generation:
 *  1. Load top survivors from the previous generation
 *  2. Mutate them via a mutation agent (POST to /responses)
 *  3. Evaluate fitness via one or more fitness agents
 *  4. Optionally judge the top cohort via a judge agent
 *  5. Write new hypotheses + updated fitness back to the store
 *  6. Select survivors via Pareto + composite fitness ranking
 *  7. Escalate any hypothesis crossing the escalation threshold
 *  8. Check convergence — stop if improvement is below threshold for N generations
 *
 * The agent also exposes 6 conversational tools so a user-facing chat UI can
 * query state, pause/resume, and force-escalate without stopping the loop.
 */

import { z } from 'zod';
import type { AgentTool } from '../agent/tools.js';
import { defineTool } from '../agent/tools.js';
import { resolveToken } from '../connectors/types.js';
import { getRequestContext, withAutonomousTrace } from '../agent/request-context.js';
import {
  addSpan,
  endSpan,
  truncate,
  agentNameFromUrl,
  traceHeadersOut,
  traceIdFromResponse,
} from '../trace.js';
import type { WorkflowEngine } from './engine.js';
import { InMemoryEngine } from './engine-memory.js';
import type { Hypothesis } from './hypothesis.js';
import { compositeFitness } from './hypothesis.js';
import { paretoFrontier, selectSurvivors } from './pareto.js';
import type { PopulationStore } from './population.js';
import type { Message, Runnable } from './types.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface EvolutionaryConfig {
  store: PopulationStore;
  populationSize: number;
  mutationBatch: number;
  mutationAgent: string;           // URL
  fitnessAgents: string[];         // URLs
  judgeAgent?: string;             // URL
  paretoObjectives: string[];
  fitnessWeights: Record<string, number>;
  maxGenerations: number;
  convergencePatience?: number;    // default 50
  convergenceThreshold?: number;   // default 0.001
  escalationThreshold?: number;    // default 0.85
  topKAdversarial?: number;        // default 0.05
  model?: string;
  instructions?: string;
  /** Per-generation timeout in milliseconds. Default: 600_000 (10 minutes). */
  generationTimeoutMs?: number;
  /**
   * Durable execution engine. If omitted, an in-process `InMemoryEngine` is
   * used — preserves the pre-durable behavior (state lost on restart). Pass a
   * `DeltaEngine` (or other backend) to survive restarts and redeploys.
   */
  engine?: WorkflowEngine;
  /**
   * If set, resume an existing run with this ID. On resume, the agent rebuilds
   * `history` and `currentGeneration` from the persisted step log and picks up
   * on the first uncompleted generation.
   */
  runId?: string;
  /** Workflow name used when creating engine run records. Default: `evolutionary`. */
  workflowName?: string;
}

export type EvolutionState = 'idle' | 'running' | 'paused' | 'converged' | 'completed' | 'failed';

export interface GenerationResult {
  generation: number;
  populationSize: number;
  bestFitness: number;
  avgFitness: number;
  paretoFrontierSize: number;
  escalated: Hypothesis[];
  wallTimeMs: number;
  converged: boolean;
}

// ---------------------------------------------------------------------------
// EvolutionaryAgent
// ---------------------------------------------------------------------------

export class EvolutionaryAgent implements Runnable {
  private _state: EvolutionState = 'idle';
  private _currentGeneration: number = 0;
  private _history: GenerationResult[] = [];
  private _loopPromise: Promise<void> | null = null;
  private _tools: AgentTool[];

  /** Current evolution state. */
  get state(): EvolutionState { return this._state; }
  /** Current generation number. */
  get currentGeneration(): number { return this._currentGeneration; }
  /** Completed generation results (read-only copy). */
  get history(): readonly GenerationResult[] { return this._history; }

  private config: EvolutionaryConfig;
  private patience: number;
  private threshold: number;
  private escalationThreshold: number;
  private topKAdversarial: number;
  private engine: WorkflowEngine;
  private workflowName: string;
  private providedRunId: string | undefined;
  private runId: string | null = null;
  private initPromise: Promise<void> | null = null;

  constructor(config: EvolutionaryConfig) {
    this.config = config;
    this.patience = config.convergencePatience ?? 50;
    this.threshold = config.convergenceThreshold ?? 0.001;
    this.escalationThreshold = config.escalationThreshold ?? 0.85;
    this.topKAdversarial = config.topKAdversarial ?? 0.05;
    this.engine = config.engine ?? new InMemoryEngine();
    this.workflowName = config.workflowName ?? 'evolutionary';
    this.providedRunId = config.runId;
    this._tools = this.buildTools();
  }

  // ---------------------------------------------------------------------------
  // Runnable interface
  // ---------------------------------------------------------------------------

  async run(_messages: Message[]): Promise<string> {
    await this.ensureInitialized();
    if (this._state === 'idle') {
      this.startLoop();
      return `Evolution started. Running generation ${this._currentGeneration} of ${this.config.maxGenerations}.`;
    }
    return this.stateSummary();
  }

  /**
   * Open (or re-open) the run with the engine and, on resume, rebuild
   * `history` and `currentGeneration` from the persisted `finalize-*` steps.
   * Idempotent — safe to call multiple times; the work happens once.
   */
  private ensureInitialized(): Promise<void> {
    if (this.initPromise) return this.initPromise;
    this.initPromise = (async () => {
      const input = {
        populationSize: this.config.populationSize,
        mutationBatch: this.config.mutationBatch,
        maxGenerations: this.config.maxGenerations,
        paretoObjectives: this.config.paretoObjectives,
        fitnessWeights: this.config.fitnessWeights,
      };
      this.runId = await this.engine.startRun(this.workflowName, input, {
        runId: this.providedRunId,
      });

      // Replay finalized generations to rebuild in-memory history.
      const snapshot = await this.engine.getRun(this.runId);
      if (snapshot) {
        const finalized = snapshot.steps
          .filter((s) => s.stepKey.startsWith('finalize-') && s.status === 'completed')
          .map((s) => s.output as GenerationResult)
          .sort((a, b) => a.generation - b.generation);
        if (finalized.length > 0) {
          this._history = finalized;
          this._currentGeneration = finalized[finalized.length - 1].generation + 1;
        }
      }
    })();
    return this.initPromise;
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    yield await this.run(messages);
  }

  collectTools(): AgentTool[] {
    return this._tools;
  }

  // ---------------------------------------------------------------------------
  // Loop control
  // ---------------------------------------------------------------------------

  getState(): EvolutionState {
    return this._state;
  }

  startLoop(): void {
    this._state = 'running';
    this._loopPromise = (async () => {
      await this.ensureInitialized();
      await this.runLoop();
    })().catch((err) => {
      this._state = 'failed';
      console.error('[EvolutionaryAgent] loop crashed:', err);
    });
  }

  pauseLoop(): void {
    this._state = 'paused';
  }

  resumeLoop(): void {
    if (this._state === 'paused' || this._state === 'failed') {
      this._state = 'running';
      this._loopPromise = (async () => {
        // Re-open the persisted run so its status flips back to 'running'.
        if (this.runId) {
          await this.engine.startRun(this.workflowName, {}, { runId: this.runId });
        }
        await this.runLoop();
      })().catch((err) => {
        this._state = 'failed';
        console.error('[EvolutionaryAgent] loop crashed:', err);
      });
    }
  }

  /**
   * Check convergence: returns true when the last `patience` entries in
   * fitnessHistory have a range (max - min) smaller than threshold.
   */
  checkConvergence(fitnessHistory: Array<{ generation: number; best: number; avg: number }>): boolean {
    if (fitnessHistory.length < this.patience) return false;
    const recent = fitnessHistory.slice(-this.patience);
    const bests = recent.map((r) => r.best);
    const range = Math.max(...bests) - Math.min(...bests);
    return range < this.threshold;
  }

  // ---------------------------------------------------------------------------
  // Private: main loop
  // ---------------------------------------------------------------------------

  private async runLoop(): Promise<void> {
    while (this._state === 'running' && this._currentGeneration < this.config.maxGenerations) {
      const gen = this._currentGeneration;
      const timeoutMs = this.config.generationTimeoutMs ?? 600_000;
      const generationTimeout = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error(`Generation ${gen} timed out after ${timeoutMs}ms`)), timeoutMs),
      );

      try {
        // Wrap each generation as its own trace so background-loop activity
        // (mutate / evaluate / judge / agent_call spans) shows up in /_apx/traces.
        const result = await withAutonomousTrace(
          this.workflowName,
          `generation ${gen}`,
          () => Promise.race([this.runGeneration(gen), generationTimeout]),
        );
        this._history.push(result);
        this._currentGeneration++;

        if (result.converged) {
          this._state = 'converged';
          break;
        }
      } catch (err) {
        console.error(`[EvolutionaryAgent] Generation ${gen} failed:`, err);
        // Treat timed-out / failed generation as a skip — advance and continue.
        this._currentGeneration++;
        continue;
      }
    }

    if (this._state === 'running') {
      this._state = 'completed';
    }

    // Persist the terminal (or paused) state on the engine so it survives restart.
    if (this.runId) {
      const status = this._state === 'idle' ? 'completed' : this._state;
      await this.engine.finishRun(this.runId, status);
    }
  }

  private async runGeneration(gen: number): Promise<GenerationResult> {
    const startTime = Date.now();
    const runId = this.runId;
    if (!runId) {
      throw new Error('runGeneration called before ensureInitialized');
    }

    // Each phase below is wrapped in engine.step so a completed phase replays
    // from cache on resume instead of re-invoking the handler.

    // 1. Load survivors from previous generation
    const prevGen = gen > 0 ? gen - 1 : 0;
    const parents = await this.engine.step<Hypothesis[]>(runId, `load-${gen}`, () =>
      this.config.store.loadTopSurvivors(
        prevGen,
        this.config.populationSize,
        this.config.fitnessWeights,
      ),
    );

    // If no parents in store (bootstrap), call the mutation agent with an empty
    // parent list to produce seed hypotheses. The mutation agent is expected to
    // generate initial random hypotheses when given no parents.
    let candidates: Hypothesis[] = [];
    if (parents.length > 0) {
      candidates = await this.engine.step<Hypothesis[]>(runId, `mutate-${gen}`, () =>
        this.mutate(parents, gen),
      );
    } else {
      candidates = await this.engine.step<Hypothesis[]>(runId, `seed-${gen}`, () =>
        this.mutate([], gen),
      );
    }

    // If mutation/seeding returned nothing, skip this generation.
    if (candidates.length === 0) {
      return this.engine.step<GenerationResult>(runId, `finalize-${gen}`, async () => ({
        generation: gen,
        populationSize: 0,
        bestFitness: 0,
        avgFitness: 0,
        paretoFrontierSize: 0,
        escalated: [],
        wallTimeMs: Date.now() - startTime,
        converged: false,
      }));
    }

    // 2. Evaluate fitness
    const evaluated = await this.engine.step<Hypothesis[]>(runId, `evaluate-${gen}`, () =>
      this.evaluate(candidates),
    );

    // 3. Judge the top cohort
    const judged = await this.engine.step<Hypothesis[]>(runId, `judge-${gen}`, () =>
      this.judge(evaluated),
    );

    // 4. Write to store — returned value is a no-op, but caching still avoids
    // redundant writes on replay.
    await this.engine.step<null>(runId, `write-${gen}`, async () => {
      await this.config.store.writeHypotheses(judged);
      return null;
    });

    // 5-7. Select, escalate, convergence check, and stats computation all
    // collapse into one `finalize` step whose output is the GenerationResult
    // we need for `history` reconstruction on resume.
    return this.engine.step<GenerationResult>(runId, `finalize-${gen}`, async () => {
      const survivors = selectSurvivors(
        judged,
        this.config.paretoObjectives,
        this.config.fitnessWeights,
        this.config.populationSize,
      );

      const escalated = survivors.filter(
        (h) => compositeFitness(h, this.config.fitnessWeights) >= this.escalationThreshold,
      );
      if (escalated.length > 0) {
        await this.config.store.flagForReview(escalated.map((h) => h.id));
        for (const h of escalated) {
          h.flagged_for_review = true;
        }
      }

      const fitnessHistory = await this.config.store.getFitnessHistory(
        this.patience,
        this.config.fitnessWeights,
      );
      const converged = this.checkConvergence(fitnessHistory);

      const scores = survivors.map((h) => compositeFitness(h, this.config.fitnessWeights));
      const bestFitness = scores.length > 0 ? Math.max(...scores) : 0;
      const avgFitness = scores.length > 0 ? scores.reduce((s, v) => s + v, 0) / scores.length : 0;
      const frontier = paretoFrontier(survivors, this.config.paretoObjectives);

      return {
        generation: gen,
        populationSize: survivors.length,
        bestFitness,
        avgFitness,
        paretoFrontierSize: frontier.length,
        escalated,
        wallTimeMs: Date.now() - startTime,
        converged,
      };
    });
  }

  // ---------------------------------------------------------------------------
  // Private: agent calls
  // ---------------------------------------------------------------------------

  private async mutate(parents: Hypothesis[], generation: number): Promise<Hypothesis[]> {
    const payload = {
      parents,
      generation,
      batch_size: this.config.mutationBatch,
      instructions: this.config.instructions,
    };

    const response = await this.callAgent(this.config.mutationAgent, payload);

    // Parse response — expect array of Hypothesis-like objects
    try {
      if (Array.isArray(response)) {
        return response as Hypothesis[];
      }
      if (typeof response === 'string') {
        return JSON.parse(response) as Hypothesis[];
      }
      if (response && typeof response === 'object' && 'hypotheses' in response) {
        return (response as { hypotheses: Hypothesis[] }).hypotheses;
      }
    } catch {
      // Fall through — return empty
    }
    return [];
  }

  private async evaluate(candidates: Hypothesis[]): Promise<Hypothesis[]> {
    const evaluated = [...candidates];

    for (const agentUrl of this.config.fitnessAgents) {
      const results = await Promise.allSettled(
        evaluated.map(async (candidate) => {
          const response = await this.callAgent(agentUrl, { hypothesis: candidate });
          return { id: candidate.id, scores: response };
        }),
      );

      for (let i = 0; i < results.length; i++) {
        const result = results[i];
        if (result.status === 'fulfilled') {
          const { scores } = result.value;
          if (scores && typeof scores === 'object' && !Array.isArray(scores)) {
            // Merge score keys into candidate fitness
            const fitnessUpdate = scores as Record<string, unknown>;
            const numericScores: Record<string, number> = {};
            for (const [k, v] of Object.entries(fitnessUpdate)) {
              if (typeof v === 'number') {
                numericScores[k] = v;
              }
            }
            evaluated[i] = {
              ...evaluated[i],
              fitness: { ...evaluated[i].fitness, ...numericScores },
            };
          }
        }
      }
    }

    return evaluated;
  }

  private async judge(evaluated: Hypothesis[]): Promise<Hypothesis[]> {
    if (!this.config.judgeAgent) return evaluated;

    // Judge the top 20% by composite fitness
    const sorted = [...evaluated].sort(
      (a, b) => compositeFitness(b, this.config.fitnessWeights) - compositeFitness(a, this.config.fitnessWeights),
    );
    const topCount = Math.max(1, Math.ceil(sorted.length * 0.2));
    const topCohort = sorted.slice(0, topCount);
    const bottomCohort = sorted.slice(topCount);

    const judgedTop = await Promise.allSettled(
      topCohort.map(async (candidate) => {
        const response = await this.callAgent(this.config.judgeAgent!, { hypothesis: candidate });
        if (response && typeof response === 'object' && !Array.isArray(response)) {
          const scores = response as Record<string, unknown>;
          const agentEvalScores: Record<string, number> = {};
          for (const [k, v] of Object.entries(scores)) {
            if (typeof v === 'number') {
              agentEvalScores[`agent_eval_${k}`] = v;
            }
          }
          return { ...candidate, fitness: { ...candidate.fitness, ...agentEvalScores } };
        }
        return candidate;
      }),
    );

    const mergedTop = judgedTop.map((r, i) =>
      r.status === 'fulfilled' ? r.value : topCohort[i],
    );

    return [...mergedTop, ...bottomCohort];
  }

  private async callAgent(url: string, payload: unknown, retries = 3): Promise<unknown> {
    const body = {
      input: [
        {
          role: 'user',
          content: JSON.stringify(payload),
        },
      ],
    };

    const trace = getRequestContext()?.trace;
    const span = trace
      ? addSpan(trace, {
          type: 'agent_call',
          name: agentNameFromUrl(url),
          input: truncate(payload),
          metadata: { childUrl: url, attempts: 0 },
        })
      : null;

    const callHeaders: Record<string, string> = {
      'Content-Type': 'application/json',
      ...traceHeadersOut(trace),
    };
    try {
      const callToken = await resolveToken();
      callHeaders.Authorization = `Bearer ${callToken}`;
    } catch (err) {
      console.warn(`[callAgent] No auth token available for ${url}:`, (err as Error).message);
    }

    try {
      const result = await this.fetchWithRetries(url, body, callHeaders, retries, span);
      if (span) span.output = truncate(result);
      return result;
    } catch (err) {
      if (span?.metadata) span.metadata.error = (err as Error).message;
      throw err;
    } finally {
      if (span) endSpan(span);
    }
  }

  /**
   * Inner retry loop, extracted so the outer callAgent can wrap span lifecycle
   * in a clean try/finally without duplicating exit points.
   */
  private async fetchWithRetries(
    url: string,
    body: unknown,
    callHeaders: Record<string, string>,
    retries: number,
    span: { metadata?: Record<string, unknown> } | null,
  ): Promise<unknown> {
    let lastError: Error | null = null;
    for (let attempt = 0; attempt <= retries; attempt++) {
      if (span?.metadata) span.metadata.attempts = attempt + 1;
      if (attempt > 0) {
        const delay = Math.min(1000 * 2 ** (attempt - 1), 30_000);
        console.warn(`[callAgent] Retry ${attempt}/${retries} for ${url} after ${delay}ms`);
        await new Promise((r) => setTimeout(r, delay));
      }

      let response: globalThis.Response;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 120_000);
      try {
        response = await fetch(`${url}/api/responses`, {
          method: 'POST',
          headers: callHeaders,
          body: JSON.stringify(body),
          signal: controller.signal,
        });
      } catch (err) {
        clearTimeout(timer);
        lastError = new Error(`Agent call to ${url} network error: ${(err as Error).message}`);
        continue;
      }
      clearTimeout(timer);

      const childTraceId = traceIdFromResponse(response);
      if (childTraceId && span?.metadata) span.metadata.childTraceId = childTraceId;

      // Retryable server errors (502, 503, 429)
      if (response.status === 502 || response.status === 503 || response.status === 429) {
        const text = await response.text();
        lastError = new Error(`Agent call to ${url} failed ${response.status}: ${text.slice(0, 500)}`);
        continue;
      }

      if (!response.ok) {
        const text = await response.text();
        throw new Error(`Agent call to ${url} failed ${response.status}: ${text.slice(0, 500)}`);
      }

      const contentType = response.headers.get('content-type') ?? '';
      if (!contentType.includes('application/json')) {
        const text = await response.text();
        throw new Error(
          `Agent call to ${url} returned ${contentType || 'no content-type'} instead of JSON ` +
          `(likely an auth redirect). Body: ${text.slice(0, 200)}`,
        );
      }

      const data = await response.json() as unknown;

      // Unwrap Responses API envelope: { output_text: "..." } or { output: [...] }
      let result: unknown = data;
      if (data && typeof data === 'object' && !Array.isArray(data)) {
        const envelope = data as Record<string, unknown>;
        if ('output_text' in envelope && typeof envelope['output_text'] === 'string') {
          result = envelope['output_text'];
        } else if ('output' in envelope) {
          result = envelope['output'];
        } else if ('content' in envelope) {
          result = envelope['content'];
        } else if ('result' in envelope) {
          result = envelope['result'];
        }
      }

      // If result is a string, attempt JSON parse — LLM agents often return
      // JSON inside markdown fences or with surrounding text.
      if (typeof result === 'string') {
        const text = result.trim();
        // Try direct parse first
        if (text.startsWith('{') || text.startsWith('[')) {
          try { return JSON.parse(text); } catch { /* fall through */ }
        }
        // Extract from markdown code fences: ```json ... ``` or ``` ... ```
        const fenceMatch = text.match(/```(?:json)?\s*\n?([\s\S]*?)\n?```/);
        if (fenceMatch) {
          try { return JSON.parse(fenceMatch[1].trim()); } catch { /* fall through */ }
        }
        // Last resort: find the first [ or { and try to parse from there
        const jsonStart = text.search(/[\[{]/);
        if (jsonStart >= 0) {
          try { return JSON.parse(text.slice(jsonStart)); } catch { /* fall through */ }
        }
      }

      return result;
    }

    // All retries exhausted
    throw lastError ?? new Error(`Agent call to ${url} failed after ${retries} retries`);
  }

  // ---------------------------------------------------------------------------
  // Private: tools
  // ---------------------------------------------------------------------------

  private buildTools(): AgentTool[] {
    return [
      defineTool({
        name: 'evolution_status',
        description: 'Get the current status of the evolutionary loop',
        parameters: z.object({}),
        handler: async () => ({
          state: this._state,
          currentGeneration: this._currentGeneration,
          maxGenerations: this.config.maxGenerations,
          historyLength: this._history.length,
        }),
      }),

      defineTool({
        name: 'best_hypothesis',
        description: 'Get the best hypothesis from the most recent completed generation',
        parameters: z.object({}),
        handler: async () => {
          if (this._history.length === 0) return { error: 'No generations completed yet' };
          const lastGen = this._currentGeneration > 0 ? this._currentGeneration - 1 : 0;
          const population = await this.config.store.loadTopSurvivors(
            lastGen,
            1,
            this.config.fitnessWeights,
          );
          return population[0] ?? { error: 'No hypotheses found' };
        },
      }),

      defineTool({
        name: 'generation_summary',
        description: 'Get a summary of results for a specific generation',
        parameters: z.object({
          generation: z.number().int().min(0),
        }),
        handler: async ({ generation }) => {
          const result = this._history.find((r) => r.generation === generation);
          if (!result) return { error: `No results for generation ${generation}` };
          return result;
        },
      }),

      defineTool({
        name: 'pause_evolution',
        description: 'Pause the evolutionary loop after the current generation completes',
        parameters: z.object({}),
        handler: async () => {
          this.pauseLoop();
          return { success: true, state: this._state };
        },
      }),

      defineTool({
        name: 'resume_evolution',
        description: 'Resume the evolutionary loop if it is paused',
        parameters: z.object({}),
        handler: async () => {
          this.resumeLoop();
          return { success: true, state: this._state };
        },
      }),

      defineTool({
        name: 'force_escalate',
        description: 'Force-escalate the top N hypotheses from the current generation for human review',
        parameters: z.object({
          topN: z.number().int().min(1).default(5),
        }),
        handler: async ({ topN }) => {
          const lastGen = this._currentGeneration > 0 ? this._currentGeneration - 1 : 0;
          const top = await this.config.store.loadTopSurvivors(
            lastGen,
            topN,
            this.config.fitnessWeights,
          );
          if (top.length === 0) return { error: 'No hypotheses to escalate', escalated: [] };
          await this.config.store.flagForReview(top.map((h) => h.id));
          return { escalated: top.map((h) => h.id), count: top.length };
        },
      }),
    ];
  }

  // ---------------------------------------------------------------------------
  // Private: helpers
  // ---------------------------------------------------------------------------

  private stateSummary(): string {
    const last = this._history[this._history.length - 1];
    const parts = [
      `State: ${this._state}`,
      `Generation: ${this._currentGeneration}/${this.config.maxGenerations}`,
    ];
    if (last) {
      parts.push(`Best fitness: ${last.bestFitness.toFixed(4)}`);
      parts.push(`Avg fitness: ${last.avgFitness.toFixed(4)}`);
      parts.push(`Pareto frontier size: ${last.paretoFrontierSize}`);
      parts.push(`Escalated: ${last.escalated.length}`);
    }
    return parts.join('\n');
  }
}
