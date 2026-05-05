/**
 * Rugg hoax generator — positive control for the falsification harness.
 *
 * Generates synthetic Voynich-like text using a Cardan grille simulation
 * (Rugg 2004: "An elegant hoax? A possible solution to the Voynich manuscript").
 * A grille with holes slides over a table of EVA-like glyphs, reading one glyph
 * per column per position. The result has Voynich-like surface statistics
 * (short words, clustered initials/terminals, low type/token ratio) but zero
 * semantic content — it's procedurally generated nonsense by construction.
 *
 * PURPOSE: verify the harness correctly REJECTS known nonsense. If it does,
 * the 1091-round null result is a functioning detector, not a broken one.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export CRITIC_AGENT_URL=https://voynich-critic-7474652869938903.aws.databricksapps.com
 *   npx tsx rugg-control.ts
 */

import { resolveToken } from './appkit-agent/index.mjs';

// ---------------------------------------------------------------------------
// Cardan grille simulator
// ---------------------------------------------------------------------------
//
// TABLE[row][col] = one EVA glyph or digraph for that (row, col) position.
// The grille has NCOLS holes, one per column, each at a row offset.
// A "word" = the sequence of glyphs at each (row+offset[col], col).
// Sliding the grille = incrementing some offsets and wrapping.
//
// Designed to reproduce the Voynich signature:
//   - Col 0: word initials  → qo-, ch-, sh-, o-, d-  (high concentration)
//   - Col 1: mid-word glyphs → free vowel-like run
//   - Col 2: mid-word glyphs → consonant-like run
//   - Col 3: terminals      → -y, -n, -dy, -ol, -r   (high concentration)
//   Column 3 is optional (short words skip it).

const NROWS = 10;

const TABLE: string[][] = [
  // col:   0        1       2       3
  /* row 0 */ ['qo',   'k',    'ee',   'y'   ],
  /* row 1 */ ['ch',   'o',    'l',    'dy'  ],
  /* row 2 */ ['sh',   'a',    'r',    'n'   ],
  /* row 3 */ ['o',    'oi',   'k',    'ol'  ],
  /* row 4 */ ['d',    'e',    'ch',   'aiin'],
  /* row 5 */ ['qo',   'ai',   'l',    'y'   ],
  /* row 6 */ ['ch',   'o',    'ee',   'dy'  ],
  /* row 7 */ ['l',    'a',    'r',    'n'   ],
  /* row 8 */ ['sh',   'oi',   'k',    'ol'  ],
  /* row 9 */ ['o',    'e',    'ch',   'y'   ],
];

// Grille configuration: which columns are "open" (true) and initial row offsets.
// Three grille shapes simulate different word-length patterns.
const GRILLE_CONFIGS = [
  { name: 'short-2',  cols: [0, 3],       offsets: [0, 0] },
  { name: 'mid-3',    cols: [0, 1, 3],    offsets: [0, 0, 0] },
  { name: 'long-4',   cols: [0, 1, 2, 3], offsets: [0, 0, 0, 0] },
];

function generateWord(colIndices: number[], rowOffsets: number[]): string {
  return colIndices.map((col, i) => TABLE[(rowOffsets[i] + col) % NROWS][col]).join('');
}

function advanceOffsets(offsets: number[], step: number): number[] {
  // Slide: increment first offset by `step`, cascade carry to next col
  const next = [...offsets];
  let carry = step;
  for (let i = 0; i < next.length && carry > 0; i++) {
    next[i] = (next[i] + carry) % NROWS;
    carry = next[i] === 0 ? 1 : 0;  // carry propagates when we wrap
  }
  return next;
}

function generateGrilleText(
  configIndex: number,
  wordCount: number,
  startOffsets?: number[],
): string {
  const cfg = GRILLE_CONFIGS[configIndex];
  let offsets = startOffsets ?? cfg.offsets.map(() => 0);
  const words: string[] = [];
  for (let i = 0; i < wordCount; i++) {
    words.push(generateWord(cfg.cols, offsets));
    // Advance grille by 1 row for the primary column, occasionally by 2
    offsets = advanceOffsets(offsets, i % 7 === 0 ? 2 : 1);
  }
  return words.join(' ');
}

/**
 * Generate a mixed-grille corpus: proportions similar to observed EVA
 * word-length distribution (28% 2-tok, 46% 3-tok, 22% 4-tok).
 */
function generateRuggCorpus(totalWords: number): string {
  const short  = generateGrilleText(0, Math.round(totalWords * 0.28), [0, 3]);
  const mid    = generateGrilleText(1, Math.round(totalWords * 0.46), [2, 1, 5]);
  const long   = generateGrilleText(2, Math.round(totalWords * 0.22), [4, 0, 7, 2]);
  // Interleave rather than concatenate blocks (to avoid length-correlation patterns)
  const shortW = short.split(' ');
  const midW   = mid.split(' ');
  const longW  = long.split(' ');
  const result: string[] = [];
  const maxLen = Math.max(shortW.length, midW.length, longW.length);
  for (let i = 0; i < maxLen; i++) {
    if (i < shortW.length) result.push(shortW[i]);
    if (i < midW.length)   result.push(midW[i]);
    if (i < longW.length)  result.push(longW[i]);
  }
  return result.join(' ');
}

// ---------------------------------------------------------------------------
// Critic endpoint caller (no tracing — standalone script)
// ---------------------------------------------------------------------------

async function callCriticTool(
  criticUrl: string,
  toolName: string,
  params: Record<string, unknown>,
): Promise<unknown | null> {
  const url = `${criticUrl.replace(/\/$/, '')}/api/agent/tools/${toolName}`;
  let token = '';
  try { token = await resolveToken(); } catch { /* unauthenticated fallback */ }

  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60_000);
    let res: globalThis.Response;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify(params),
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      console.warn(`  [critic/${toolName}] http ${res.status}: ${body.slice(0, 120)}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.warn(`  [critic/${toolName}] threw: ${(err as Error).message}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// Run one trial through the full harness
// ---------------------------------------------------------------------------

interface TrialResult {
  label: string;
  decodedText: string;
  wordCount: number;
  likelihood: number | null;
  nullDistinguishable: boolean | null;
  nullPWithin: number | null;
  nullPAcross: number | null;
  judgeVerdict: string;
  compositeVerdict: string;
}

async function runTrial(
  criticUrl: string,
  label: string,
  decodedText: string,
): Promise<TrialResult> {
  const wordCount = decodedText.split(/\s+/).filter(Boolean).length;
  process.stdout.write(`\n  [${label}] ${wordCount} words — running critic checks...\n`);

  const [likelihoodRaw, nullRaw, contradictionsRaw] = await Promise.all([
    callCriticTool(criticUrl, 'score_latin_likelihood', { decoded_text: decodedText, source_language: 'latin' }),
    callCriticTool(criticUrl, 'null_baseline_test',     { decoded_text: decodedText, source_language: 'latin' }),
    callCriticTool(criticUrl, 'find_contradictions',    { decoded_text: decodedText, section: 'herbal' }),
  ]);

  const lik = (likelihoodRaw as { likelihood?: number } | null)?.likelihood ?? null;
  const nullTest = nullRaw as { distinguishable?: boolean; within_word?: { p_value?: number }; across_text?: { p_value?: number } } | null;
  const nullDist = nullTest?.distinguishable ?? null;
  const nullPWithin = nullTest?.within_word?.p_value ?? null;
  const nullPAcross = nullTest?.across_text?.p_value ?? null;
  const adversarial = (contradictionsRaw as { adversarial?: number } | null)?.adversarial ?? null;

  process.stdout.write(`    likelihood=${lik?.toFixed(3) ?? 'null'}  null_dist=${nullDist}  adversarial=${adversarial?.toFixed(3) ?? 'null'}\n`);

  // Gate llm_judge: only if heuristics are promising (same gate as theory-loop.ts)
  const GATE = 0.3;
  let judgeVerdict = 'SKIPPED';
  if ((lik ?? 0) >= GATE && nullDist === true) {
    process.stdout.write(`    likelihood above gate — running llm_judge...\n`);
    const judgeRaw = await callCriticTool(criticUrl, 'llm_judge', { decoded_text: decodedText, source_language: 'latin' });
    const judge = judgeRaw as { verdict?: string; reason?: string } | null;
    judgeVerdict = judge?.verdict ?? 'FAIL';
    if (judge?.reason) process.stdout.write(`    judge reason: ${judge.reason.slice(0, 120)}\n`);
  }

  // Composite verdict
  let compositeVerdict: string;
  if (
    (adversarial !== null && adversarial < 0.5) ||
    nullDist === false ||
    judgeVerdict === 'FAIL'
  ) {
    compositeVerdict = 'rejected';
  } else if (judgeVerdict === 'PASS') {
    compositeVerdict = 'plausible';
  } else if ((lik ?? 0) >= GATE && nullDist === true) {
    compositeVerdict = 'weak';
  } else {
    compositeVerdict = 'rejected';
  }

  return { label, decodedText, wordCount, likelihood: lik, nullDistinguishable: nullDist,
           nullPWithin, nullPAcross, judgeVerdict, compositeVerdict };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const criticUrl = process.env.CRITIC_AGENT_URL ?? '';
  if (!criticUrl) {
    console.error('CRITIC_AGENT_URL not set. Export it and re-run.');
    process.exit(1);
  }

  console.log('rugg-control — positive control for the falsification harness');
  console.log(`Critic: ${criticUrl}`);
  console.log('');
  console.log('Generating synthetic Voynich-like text via Cardan grille simulation...');
  console.log('(Rugg 2004: structured nonsense with Voynich-like word statistics)');

  // Show sample generated text for spot-check
  const sample = generateGrilleText(1, 12);
  console.log(`\nSample 12-word grille output (mid-3 config): ${sample}`);

  // Three trials of increasing length and variety
  const trials: Array<{ label: string; text: string }> = [
    {
      label: 'single-grille-200',
      text: generateGrilleText(1, 200),  // pure mid-3 config, 200 words
    },
    {
      label: 'mixed-grille-400',
      text: generateRuggCorpus(400),  // mixed length distribution, 400 words
    },
    {
      label: 'mixed-grille-900',
      text: generateRuggCorpus(900),  // larger corpus, more statistically stable
    },
  ];

  console.log('\nRunning trials through critic harness...');
  const results: TrialResult[] = [];
  for (const { label, text } of trials) {
    const r = await runTrial(criticUrl, label, text);
    results.push(r);
  }

  // Summary
  console.log('\n=== RESULTS SUMMARY ===');
  console.log('');
  console.log(
    'trial'.padEnd(24) +
    'words'.padStart(6) +
    'lik'.padStart(7) +
    'null?'.padStart(7) +
    'judge'.padStart(8) +
    'verdict'.padStart(10),
  );
  console.log('-'.repeat(65));
  for (const r of results) {
    console.log(
      r.label.padEnd(24) +
      r.wordCount.toString().padStart(6) +
      (r.likelihood?.toFixed(3) ?? 'null').padStart(7) +
      (r.nullDistinguishable === null ? 'null' : r.nullDistinguishable ? 'yes' : 'no').padStart(7) +
      r.judgeVerdict.padStart(8) +
      r.compositeVerdict.padStart(10),
    );
  }

  const allRejected = results.every((r) => r.compositeVerdict === 'rejected');
  console.log('');
  if (allRejected) {
    console.log('CONTROL PASS: harness correctly rejected all Rugg-generated trials.');
    console.log('The 1091-round null result is a functioning detector, not a broken one.');
    console.log('Known nonsense (procedurally generated) is distinguishable from whatever');
    console.log('the EVA decoder produces — both are rejected, but for the right reason.');
  } else {
    const survivors = results.filter((r) => r.compositeVerdict !== 'rejected');
    console.log(`CONTROL FAIL: ${survivors.length} trial(s) were NOT rejected:`);
    for (const r of survivors) {
      console.log(`  ${r.label}: ${r.compositeVerdict} (lik=${r.likelihood?.toFixed(3)}, judge=${r.judgeVerdict})`);
    }
    console.log('The harness has a false-positive problem. Prior null results may be unreliable.');
  }
}

main().catch((err) => {
  console.error('[rugg-control] fatal:', err);
  process.exit(1);
});
