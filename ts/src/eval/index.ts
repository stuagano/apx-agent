/**
 * eval — Eval bridge and harness for appkit-agent /responses endpoints.
 *
 * @example
 * import { createPredictFn, runEval } from 'appkit-agent/eval';
 *
 * const predict = createPredictFn('http://localhost:8000', { token: process.env.AGENT_TOKEN });
 *
 * const summary = await runEval(predict, [
 *   { input: 'What is 2+2?', expected: '4', tags: ['math'] },
 *   { input: 'Capital of France?', expected: 'Paris' },
 * ], { concurrency: 3, verbose: true });
 *
 * console.log(`Passed: ${summary.passed}/${summary.total} | avg ${summary.avg_latency_ms}ms`);
 */

export { createPredictFn } from './predict.js';
export type { PredictFn, PredictOptions, Message, PredictInput } from './predict.js';

export { runEval } from './harness.js';
export type { EvalCase, EvalResult, RunEvalOptions, EvalSummary } from './harness.js';
