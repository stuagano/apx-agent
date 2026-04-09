/**
 * harness.ts — Eval harness for /responses-compatible agents.
 *
 * Runs a set of eval cases against a predict function, measures latency,
 * checks pass/fail, and reports a summary. No MLflow dependency.
 */

import type { PredictFn } from './predict.js';

export interface EvalCase {
  /** Input to send to the agent. */
  input: string;
  /** If provided, output must include this string to pass. */
  expected?: string;
  /** Optional tags for grouping/filtering. */
  tags?: string[];
}

export interface EvalResult {
  input: string;
  output: string;
  expected?: string;
  /** True = output contains expected string. Undefined if no expected provided. */
  passed?: boolean;
  latency_ms: number;
  error?: string;
}

export interface RunEvalOptions {
  /** Maximum number of cases to run in parallel. Defaults to 5. */
  concurrency?: number;
  /** If true, log each result to stdout as it completes. Defaults to false. */
  verbose?: boolean;
}

export interface EvalSummary {
  total: number;
  passed: number;
  failed: number;
  errored: number;
  avg_latency_ms: number;
  results: EvalResult[];
}

/**
 * Run all eval cases against the given predict function.
 *
 * Cases are executed in batches respecting the concurrency limit.
 * Latency is measured per case. Pass/fail is determined by simple
 * string inclusion of `expected` in the output.
 *
 * @example
 * const predict = createPredictFn('http://localhost:8000');
 * const summary = await runEval(predict, [
 *   { input: 'What is 2+2?', expected: '4' },
 *   { input: 'Capital of France?', expected: 'Paris' },
 * ]);
 * console.log(`Passed: ${summary.passed}/${summary.total}`);
 */
export async function runEval(
  predictFn: PredictFn,
  cases: EvalCase[],
  options: RunEvalOptions = {},
): Promise<EvalSummary> {
  const concurrency = options.concurrency ?? 5;
  const verbose = options.verbose ?? false;

  const results: EvalResult[] = [];

  // Process in batches of `concurrency`
  for (let i = 0; i < cases.length; i += concurrency) {
    const batch = cases.slice(i, i + concurrency);

    const batchResults = await Promise.all(
      batch.map(async (evalCase): Promise<EvalResult> => {
        const start = Date.now();
        try {
          const output = await predictFn(evalCase.input);
          const latency_ms = Date.now() - start;

          const passed =
            evalCase.expected !== undefined
              ? output.includes(evalCase.expected)
              : undefined;

          const result: EvalResult = {
            input: evalCase.input,
            output,
            expected: evalCase.expected,
            passed,
            latency_ms,
          };

          if (verbose) {
            const status =
              passed === undefined ? 'N/A' : passed ? 'PASS' : 'FAIL';
            process.stdout.write(
              `[${status}] ${latency_ms}ms — ${evalCase.input.slice(0, 60)}\n`,
            );
          }

          return result;
        } catch (err) {
          const latency_ms = Date.now() - start;
          const error = err instanceof Error ? err.message : String(err);

          if (verbose) {
            process.stdout.write(
              `[ERR] ${latency_ms}ms — ${evalCase.input.slice(0, 60)}: ${error}\n`,
            );
          }

          return {
            input: evalCase.input,
            output: '',
            expected: evalCase.expected,
            passed: false,
            latency_ms,
            error,
          };
        }
      }),
    );

    results.push(...batchResults);
  }

  // Compute summary
  const total = results.length;
  const errored = results.filter((r) => r.error !== undefined).length;
  const withExpected = results.filter((r) => r.expected !== undefined);
  const passed = withExpected.filter((r) => r.passed === true).length;
  const failed = withExpected.filter((r) => r.passed === false).length;
  const avg_latency_ms =
    total > 0
      ? Math.round(results.reduce((sum, r) => sum + r.latency_ms, 0) / total)
      : 0;

  const summary: EvalSummary = {
    total,
    passed,
    failed,
    errored,
    avg_latency_ms,
    results,
  };

  if (verbose) {
    process.stdout.write(
      `\nSummary: ${total} cases | ${passed} passed | ${failed} failed | ${errored} errored | avg ${avg_latency_ms}ms\n`,
    );
  }

  return summary;
}
