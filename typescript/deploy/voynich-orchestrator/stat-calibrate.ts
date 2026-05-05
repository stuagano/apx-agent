// typescript/deploy/voynich-orchestrator/stat-calibrate.ts
// End-to-end calibration for the statistical EA pipeline.
//
// Run:
//   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
//   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
//   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
//   STAT_EXECUTOR_URL=http://localhost:8005 STAT_MUTATION_URL=http://localhost:8006 \
//   CRITIC_URL=http://localhost:8003 npx tsx stat-calibrate.ts

import { rankBiserial, computeFeature, approxPValue, FINDINGS_SUMMARY } from './stat-types.ts';

function assert(cond: boolean, msg: string): void {
  if (!cond) { console.error(`  FAIL: ${msg}`); process.exitCode = 1; }
  else        { console.log (`  PASS: ${msg}`); }
}

async function callTool(baseUrl: string, tool: string, params: unknown): Promise<unknown> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  const token = process.env.DATABRICKS_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(`${baseUrl.replace(/\/$/, '')}/api/agent/tools/${tool}`, {
    method: 'POST', headers,
    body: JSON.stringify(params),
  });
  if (!res.ok) throw new Error(`${tool} returned ${res.status}: ${await res.text().then(t => t.slice(0, 200))}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Unit checks — pure functions
// ---------------------------------------------------------------------------

console.log('\n=== Unit: rankBiserial ===');
{
  const r = rankBiserial([1, 2, 3], [0, 0.5]);
  assert(r === 1.0, `all A > all B → r=1.0 (got ${r})`);
  const r2 = rankBiserial([0.1, 0.2], [0.5, 0.6]);
  assert(r2 === -1.0, `all A < all B → r=-1.0 (got ${r2})`);
  const r3 = rankBiserial([], [1, 2]);
  assert(r3 === 0, `empty groupA → r=0 (got ${r3})`);
}

console.log('\n=== Unit: computeFeature ===');
{
  const words = ['qokeedy', 'daiin', 'qochor', 'chedy', 'ol'];
  const qo = computeFeature(words, 'qo-prefix rate');
  assert(qo !== null && Math.abs(qo - 0.4) < 0.001, `qo-prefix rate = 2/5 = 0.4 (got ${qo})`);
  const unk = computeFeature(words, 'nonexistent feature');
  assert(unk === null, `unknown feature → null`);
  const short = computeFeature(words, 'short word rate');
  assert(short !== null && Math.abs(short - 0.2) < 0.001, `short word rate = 1/5 = 0.2 (got ${short})`);
}

console.log('\n=== Unit: approxPValue ===');
{
  const p = approxPValue(0.532, 33, 60);
  assert(p < 0.001, `r=0.532, n1=33, n2=60 → p < 0.001 (got ${p.toFixed(4)})`);
  const pNS = approxPValue(0.05, 10, 10);
  assert(pNS > 0.5, `r=0.05, n=10 → p > 0.5 (not significant, got ${pNS.toFixed(3)})`);
}

console.log('\n=== Unit: FINDINGS_SUMMARY ===');
assert(FINDINGS_SUMMARY.includes('qo-prefix'), 'FINDINGS_SUMMARY mentions qo-prefix');
assert(FINDINGS_SUMMARY.includes('permutation'), 'FINDINGS_SUMMARY mentions permutation');
assert(FINDINGS_SUMMARY.length > 200, 'FINDINGS_SUMMARY is substantive');

// ---------------------------------------------------------------------------
// Integration: executor
// ---------------------------------------------------------------------------

const EXECUTOR_URL = process.env.STAT_EXECUTOR_URL ?? '';
const MUTATION_URL = process.env.STAT_MUTATION_URL ?? '';
const CRITIC_URL   = process.env.CRITIC_URL ?? '';

if (!EXECUTOR_URL) {
  console.log('\nSkipping executor/mutation/critic integration tests (STAT_EXECUTOR_URL not set)');
  console.log('Set STAT_EXECUTOR_URL, STAT_MUTATION_URL, CRITIC_URL to run integration tests.');
  process.exit(process.exitCode ?? 0);
}

console.log('\n=== Integration: executor — known-good spec ===');
{
  const knownGoodSpec = {
    feature: 'qo-prefix rate',
    method: 'rank-biserial',
    family_a: 'solanaceae',
    family_b: 'all-botanical',
    rationale: 'Replication of FINDINGS.md qo-prefix result (r≈0.532)',
  };
  const result = await callTool(EXECUTOR_URL, 'execute_stat_test', { spec: knownGoodSpec }) as any;
  assert(!result.error, `known-good: no error (got: ${result.error})`);
  assert(typeof result.effect_size === 'number', `known-good: effect_size is a number (got ${result.effect_size})`);
  assert(result.effect_size > 0.3, `known-good: effect_size > 0.3 (got ${result.effect_size})`);
  assert(result.p_value < 0.05, `known-good: p_value < 0.05 (got ${result.p_value})`);
  assert(result.n_samples >= 50, `known-good: n_samples >= 50 (got ${result.n_samples})`);
}

console.log('\n=== Integration: executor — known-weak spec ===');
{
  const knownWeakSpec = {
    feature: 'short word rate',
    method: 'rank-biserial',
    family_a: 'thistle',
    family_b: 'lily-family',
    rationale: 'Random control — small families with no expected signal',
  };
  const result = await callTool(EXECUTOR_URL, 'execute_stat_test', { spec: knownWeakSpec }) as any;
  // May return error (small n) or valid result — just verify it doesn't crash
  assert(typeof result === 'object', `known-weak: returns object (got ${typeof result})`);
  if (!result.error) {
    assert(typeof result.effect_size === 'number', `known-weak: effect_size is number`);
  }
}

console.log('\n=== Integration: executor — malformed spec (empty family_b) ===');
{
  const malformedSpec = {
    feature: 'qo-prefix rate',
    method: 'rank-biserial',
    family_a: 'solanaceae',
    family_b: '',
    rationale: 'Malformed: empty family_b',
  };
  const result = await callTool(EXECUTOR_URL, 'execute_stat_test', { spec: malformedSpec }) as any;
  assert(result.error !== undefined, `malformed: returns error field (got: ${JSON.stringify(result).slice(0, 100)})`);
}

// ---------------------------------------------------------------------------
// Integration: mutation — smoke test
// ---------------------------------------------------------------------------

if (MUTATION_URL) {
  console.log('\n=== Integration: mutation — smoke test ===');
  const result = await callTool(MUTATION_URL, 'generate_hypothesis', {
    top_findings: [],
    generation: 1,
  }) as any;
  assert(typeof result.feature === 'string' && result.feature.length > 0, `mutation: feature is non-empty string`);
  assert(typeof result.method === 'string', `mutation: method is string`);
  assert(typeof result.family_a === 'string', `mutation: family_a is string`);
  assert(typeof result.family_b === 'string', `mutation: family_b is string`);
  assert(typeof result.rationale === 'string' && result.rationale.length > 0, `mutation: rationale is non-empty`);
  const knownEstablished = ['qo-prefix rate + solanaceae', '-chy suffix rate + solanaceae', '-dy suffix rate + solanaceae'];
  const proposed = `${result.feature} + ${result.family_a}`;
  assert(!knownEstablished.some(k => k === proposed), `mutation gen-1: proposes something beyond established solanaceae morphology (got: ${proposed})`);
}

// ---------------------------------------------------------------------------
// Integration: critic — score known-good finding
// ---------------------------------------------------------------------------

if (CRITIC_URL) {
  console.log('\n=== Integration: critic — score known-good finding ===');
  const knownGoodFinding = {
    spec: { feature: 'qo-prefix rate', method: 'permutation-test', family_a: 'solanaceae', family_b: 'all-botanical', rationale: 'known result' },
    effect_size: 0.532,
    p_value: 0.001,
    n_samples: 93,
    interpretation: 'solanaceae shows significantly higher qo-prefix rate vs all-botanical (r=0.532, p<0.001)',
  };
  const result = await callTool(CRITIC_URL, 'score_statistical_finding', { finding: knownGoodFinding }) as any;
  assert(typeof result.critic_score === 'number', `critic known-good: returns critic_score number`);
  assert(result.critic_score >= 0.2, `critic known-good: score >= 0.2 (valid result, low novelty, got ${result.critic_score})`);
  assert(result.critic_score <= 0.7, `critic known-good: score <= 0.7 (known result, not highly novel, got ${result.critic_score})`);
  assert(typeof result.critic_feedback === 'string', `critic known-good: returns critic_feedback string`);

  console.log('\n=== Integration: critic — score low-validity finding ===');
  const lowValidityFinding = {
    spec: { feature: 'short word rate', method: 'rank-biserial', family_a: 'rose', family_b: 'verbena', rationale: 'control' },
    effect_size: 0.05,
    p_value: 0.45,
    n_samples: 8,
    interpretation: 'no significant difference',
  };
  const result2 = await callTool(CRITIC_URL, 'score_statistical_finding', { finding: lowValidityFinding }) as any;
  assert(result2.critic_score <= 0.3, `critic low-validity: score <= 0.3 (got ${result2.critic_score})`);
}

console.log('\n=== Calibration complete ===');
console.log(`Exit code: ${process.exitCode ?? 0}`);
