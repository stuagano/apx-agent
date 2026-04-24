/**
 * Voynich Annealer — Jakobsen simulated-annealing solver as an AppKit agent.
 *
 * Tools exposed:
 *   - jakobsen_solve   : run SA on EVA text against {latin, hebrew, arabic}
 *   - compute_ic       : Index of Coincidence + adjacent-glyph repeat rate
 *   - section_verdict  : run all three languages on a section, return verdict
 *
 * Why this is a separate agent from `decipherer`:
 *   `decipherer` is the LLM-driven mutation agent (random swaps, theory
 *   proposals). Jakobsen SA is deterministic CPU work — no LLM calls, runs
 *   in seconds. Keeping it isolated means the orchestrator can call SA as a
 *   short-circuit *before* burning generations on monoalphabetic hypotheses
 *   that the SA already rejected.
 *
 * Implementation parity with python/examples/voynich/scripts/jakobsen_sa.py.
 */

import express from 'express';
import { z } from 'zod';
import {
  defineTool,
  createAgentPlugin,
  createDiscoveryPlugin,
  createDevPlugin,
} from '../../../src/index.js';

// ---------------------------------------------------------------------------
// Tokenization (matches python/examples/voynich/scripts/eva_sections.py)
// ---------------------------------------------------------------------------

const TOKEN_RE = /ch|sh|th|qo|[a-z]/g;

function tokenize(evaText: string): string[] {
  const tokens: string[] = [];
  for (const word of evaText.toLowerCase().split(/\s+/)) {
    const matches = word.match(TOKEN_RE);
    if (matches) tokens.push(...matches);
  }
  return tokens;
}

// ---------------------------------------------------------------------------
// Language models — same numeric tables as ngram_model.py
// ---------------------------------------------------------------------------

const LATIN_UNIGRAM: Record<string, number> = {
  a: 0.0814, b: 0.0157, c: 0.0306, d: 0.0273, e: 0.117, f: 0.0095,
  g: 0.0114, h: 0.0097, i: 0.1138, k: 0.0001, l: 0.0506, m: 0.0337,
  n: 0.0623, o: 0.054, p: 0.0303, q: 0.0151, r: 0.0667, s: 0.0762,
  t: 0.0805, u: 0.0848, v: 0.0091, x: 0.0061, y: 0.0008, z: 0.0002,
};

const LATIN_BIGRAM: Record<string, number> = {
  us: -1.8, um: -1.9, is: -1.7, em: -2.1, et: -2.2, ur: -2.0, in: -1.6,
  qu: -1.5, re: -2.0, st: -2.1, te: -2.1, ti: -1.8, ut: -2.3, es: -1.9,
  ra: -2.2, or: -2.2, an: -2.0, at: -2.1, io: -2.0, ic: -2.2, ar: -2.1,
  ne: -2.2, nt: -2.1, ri: -2.2, ta: -2.1, to: -2.2, de: -2.0, co: -2.1,
  ce: -2.2, le: -2.2, li: -2.2, lu: -2.4, ma: -2.2, me: -2.2, mo: -2.3,
  ni: -2.2, no: -2.2, om: -2.3, on: -2.1, op: -2.5, os: -2.2, pe: -2.3,
  po: -2.3, pr: -2.2, se: -2.0, si: -2.1, ss: -2.5, su: -2.3, ve: -2.3,
  vi: -2.4,
};

const HEBREW_UNIGRAM: Record<string, number> = {
  "'": 0.042, b: 0.047, g: 0.0118, d: 0.0278, h: 0.089, v: 0.104,
  z: 0.0095, x: 0.0188, t: 0.0145, y: 0.1052, k: 0.047, l: 0.071,
  m: 0.067, n: 0.051, s: 0.014, '`': 0.029, p: 0.019, c: 0.012,
  q: 0.014, r: 0.059, w: 0.027, T: 0.049,
};

const HEBREW_BIGRAM: Record<string, number> = {
  hv: -1.7, vh: -1.9, hy: -1.8, yh: -1.9, lh: -2.0, hl: -2.0,
  yT: -2.1, Ty: -2.0, ml: -2.1, lm: -2.2, br: -2.1, rb: -2.2,
  yk: -2.1, ky: -2.2, vy: -1.9, yv: -2.0, kn: -2.3, nk: -2.4,
  hr: -2.0, rh: -2.2, hm: -2.0, mh: -2.2, ah: -2.0, ha: -2.1,
  yn: -2.0, ny: -2.1, lk: -2.2, kl: -2.2, lT: -2.2, Tl: -2.3,
  rk: -2.3, kr: -2.3, qd: -2.4, dq: -2.5, wm: -2.2, mw: -2.3,
  rm: -2.2, mr: -2.2, lc: -2.4, cl: -2.4, ng: -2.5, gn: -2.6,
  yp: -2.4, py: -2.4, mq: -2.5, qm: -2.5, vd: -2.3,
};

const ARABIC_UNIGRAM: Record<string, number> = {
  a: 0.143, b: 0.0382, t: 0.054, T: 0.0098, j: 0.0117, H: 0.0153,
  x: 0.0084, d: 0.0345, D: 0.0036, r: 0.0628, z: 0.0083, s: 0.0274,
  S: 0.0089, c: 0.0096, C: 0.0039, W: 0.0039, Z: 0.0006, '`': 0.022,
  g: 0.0042, f: 0.0245, q: 0.0181, k: 0.0199, l: 0.102, m: 0.0612,
  n: 0.0838, h: 0.033, w: 0.0584, y: 0.0488,
};

const ARABIC_BIGRAM: Record<string, number> = {
  al: -1.4, la: -1.7, an: -1.8, na: -1.9, ma: -1.9, am: -2.0, in: -1.9,
  ni: -2.0, ar: -1.9, ra: -1.9, li: -2.0, il: -2.0, lm: -2.1, ml: -2.2,
  ya: -1.9, ay: -2.0, wa: -1.8, aw: -2.0, ha: -2.0, ah: -2.0, ka: -2.0,
  ak: -2.1, fa: -2.1, af: -2.2, ba: -2.0, ab: -2.1, ta: -1.9, at: -1.9,
  da: -2.0, ad: -2.1, qa: -2.1, aq: -2.2, sa: -2.0, as: -2.1, Sa: -2.3,
  aS: -2.4, Ha: -2.2, aH: -2.3, yn: -2.0, ny: -2.1, rn: -2.3, nr: -2.4,
  lh: -2.2, hl: -2.2, kt: -2.4, tk: -2.5,
};

const LOG_FLOOR = -12.0;

interface LangModel {
  name: string;
  alphabet: string[];          // sorted by descending unigram frequency
  unigramLog: Record<string, number>;
  bigramLog: Record<string, number>;
}

function buildModel(name: string, uni: Record<string, number>, big: Record<string, number>): LangModel {
  const total = Object.values(uni).reduce((a, b) => a + b, 0);
  const unigramLog: Record<string, number> = {};
  for (const [c, p] of Object.entries(uni)) unigramLog[c] = Math.log(p / total);
  const alphabet = Object.keys(uni).sort((a, b) => uni[b] - uni[a]);
  return { name, alphabet, unigramLog, bigramLog: big };
}

const LATIN = buildModel('latin', LATIN_UNIGRAM, LATIN_BIGRAM);
const HEBREW = buildModel('hebrew', HEBREW_UNIGRAM, HEBREW_BIGRAM);
const ARABIC = buildModel('arabic', ARABIC_UNIGRAM, ARABIC_BIGRAM);

const MODELS: Record<string, LangModel> = { latin: LATIN, hebrew: HEBREW, arabic: ARABIC };

function score(model: LangModel, plaintext: string): number {
  if (plaintext.length < 2) return 0;
  let total = 0;
  for (let i = 0; i < plaintext.length - 1; i++) {
    const pair = plaintext[i] + plaintext[i + 1];
    const v = model.bigramLog[pair];
    if (v !== undefined) {
      total += v;
    } else {
      const a = model.unigramLog[pair[0]] ?? LOG_FLOOR;
      const b = model.unigramLog[pair[1]] ?? LOG_FLOOR;
      total += (a + b) * 0.5 - 1.0;
    }
  }
  return total;
}

// ---------------------------------------------------------------------------
// IC and repeat rate
// ---------------------------------------------------------------------------

function indexOfCoincidence(tokens: string[]): number {
  if (tokens.length < 2) return 0;
  const counts: Record<string, number> = {};
  for (const t of tokens) counts[t] = (counts[t] || 0) + 1;
  const n = tokens.length;
  let num = 0;
  for (const c of Object.values(counts)) num += c * (c - 1);
  return num / (n * (n - 1));
}

function repeatRate(tokens: string[]): number {
  if (tokens.length < 2) return 0;
  let r = 0;
  for (let i = 0; i < tokens.length - 1; i++) if (tokens[i] === tokens[i + 1]) r++;
  return r / (tokens.length - 1);
}

// ---------------------------------------------------------------------------
// Jakobsen SA (port of jakobsen_sa.py)
// ---------------------------------------------------------------------------

function decode(tokens: string[], glyphs: string[], letters: string[]): string {
  const m: Record<string, string> = {};
  for (let i = 0; i < glyphs.length; i++) m[glyphs[i]] = letters[i];
  return tokens.map((t) => m[t] ?? '?').join('');
}

interface SAResult {
  language: string;
  bestKey: Record<string, string>;
  bestScore: number;
  perTokenScore: number;
  decodedSample: string;
  nGlyphs: number;
  nTokens: number;
  ic: number;
  repeatRate: number;
  restartScores: number[];
  converged: boolean;
  notes: string;
}

function runSA(
  tokens: string[],
  model: LangModel,
  iterations: number,
  restarts: number,
  tStart: number,
  tEnd: number,
  seed: number,
): SAResult {
  // Seedable PRNG (Mulberry32 — small and good enough for SA)
  function makeRng(s: number) {
    let state = s >>> 0;
    return () => {
      state = (state + 0x6D2B79F5) >>> 0;
      let t = state;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  const counts: Record<string, number> = {};
  for (const t of tokens) counts[t] = (counts[t] || 0) + 1;
  const glyphs = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
  const n = glyphs.length;
  const ic = indexOfCoincidence(tokens);
  const rr = repeatRate(tokens);

  if (n < 2) {
    return {
      language: model.name, bestKey: {}, bestScore: 0, perTokenScore: 0,
      decodedSample: '', nGlyphs: n, nTokens: tokens.length,
      ic, repeatRate: rr, restartScores: [], converged: false,
      notes: 'alphabet too small to attack',
    };
  }

  let bestOverall: string[] | null = null;
  let bestOverallScore = -Infinity;
  const restartScores: number[] = [];
  const topKeys: string[] = [];

  for (let restart = 0; restart < restarts; restart++) {
    const rng = makeRng(seed + restart * 7919);
    // Frequency-aligned seed key, possibly perturbed for restarts > 0
    const letters: string[] = [];
    for (let i = 0; i < n; i++) {
      letters.push(model.alphabet[i] ?? model.alphabet[model.alphabet.length - 1]);
    }
    for (let p = 0; p < restart * 2; p++) {
      const i = Math.floor(rng() * n);
      let j = Math.floor(rng() * n);
      if (j === i) j = (j + 1) % n;
      [letters[i], letters[j]] = [letters[j], letters[i]];
    }

    let currentScore = score(model, decode(tokens, glyphs, letters));
    let bestLocal = [...letters];
    let bestLocalScore = currentScore;

    for (let step = 0; step < iterations; step++) {
      const ratio = tEnd / tStart;
      const T = tStart * Math.pow(ratio, step / Math.max(iterations - 1, 1));
      const i = Math.floor(rng() * n);
      let j = Math.floor(rng() * n);
      if (j === i) j = (j + 1) % n;
      [letters[i], letters[j]] = [letters[j], letters[i]];
      const newScore = score(model, decode(tokens, glyphs, letters));
      const delta = newScore - currentScore;
      if (delta > 0 || rng() < Math.exp(delta / Math.max(T, 1e-9))) {
        currentScore = newScore;
        if (newScore > bestLocalScore) {
          bestLocalScore = newScore;
          bestLocal = [...letters];
        }
      } else {
        [letters[i], letters[j]] = [letters[j], letters[i]];
      }
    }

    restartScores.push(bestLocalScore);
    topKeys.push(bestLocal.join('|'));
    if (bestLocalScore > bestOverallScore) {
      bestOverallScore = bestLocalScore;
      bestOverall = bestLocal;
    }
  }

  const winner = bestOverall!;
  const keyMap: Record<string, string> = {};
  for (let i = 0; i < n; i++) keyMap[glyphs[i]] = winner[i];
  const decoded = decode(tokens, glyphs, winner);

  // Convergence: do at least half the restarts agree on the same key string?
  const keyCounts: Record<string, number> = {};
  for (const k of topKeys) keyCounts[k] = (keyCounts[k] || 0) + 1;
  const maxAgree = Math.max(...Object.values(keyCounts));
  const converged = maxAgree >= Math.max(2, Math.floor(restarts / 2) + 1);

  const perToken = bestOverallScore / Math.max(tokens.length - 1, 1);
  const notes: string[] = [];
  if (ic < 0.045) notes.push(`low IC (${ic.toFixed(3)})`);
  else if (ic > 0.085) notes.push(`high IC (${ic.toFixed(3)})`);
  if (rr > 0.01) notes.push(`high repeat-rate (${rr.toFixed(3)})`);
  if (!converged) notes.push('restarts disagreed');

  return {
    language: model.name,
    bestKey: keyMap,
    bestScore: bestOverallScore,
    perTokenScore: perToken,
    decodedSample: decoded.slice(0, 200),
    nGlyphs: n,
    nTokens: tokens.length,
    ic,
    repeatRate: rr,
    restartScores,
    converged,
    notes: notes.length ? notes.join('; ') : 'ok',
  };
}

// ---------------------------------------------------------------------------
// Tools
// ---------------------------------------------------------------------------

const jakobsenSolve = defineTool({
  name: 'jakobsen_solve',
  description:
    "Run Jakobsen's simulated-annealing monoalphabetic-substitution solver on a " +
    'block of EVA text against one candidate language. Returns the best key, the ' +
    'decoded sample, per-token bigram log-score, and convergence diagnostics. ' +
    'Use this BEFORE spending generations evolving substitution hypotheses — if ' +
    'SA cannot converge, the cipher is unlikely to be a simple substitution.',
  parameters: z.object({
    eva_text: z.string().describe('Raw EVA-transliterated text (whitespace-separated words).'),
    language: z.enum(['latin', 'hebrew', 'arabic']),
    iterations: z.number().int().positive().default(20_000).optional(),
    restarts: z.number().int().positive().default(4).optional(),
    seed: z.number().int().default(42).optional(),
  }),
  handler: async ({ eva_text, language, iterations, restarts, seed }) => {
    const tokens = tokenize(eva_text);
    return runSA(
      tokens,
      MODELS[language],
      iterations ?? 20_000,
      restarts ?? 4,
      10.0,
      0.01,
      seed ?? 42,
    );
  },
});

const computeIc = defineTool({
  name: 'compute_ic',
  description:
    'Compute Index of Coincidence and adjacent-glyph repeat rate on a block of ' +
    'EVA text. Plaintext IC is typically 0.06-0.075; random text scores ~1/N. ' +
    "Voynich's IC ≈ 0.08 is in plaintext range, but its high repeat rate is not.",
  parameters: z.object({
    eva_text: z.string(),
  }),
  handler: async ({ eva_text }) => {
    const tokens = tokenize(eva_text);
    const ic = indexOfCoincidence(tokens);
    const rr = repeatRate(tokens);
    const distinct = new Set(tokens).size;
    return {
      n_tokens: tokens.length,
      n_distinct_glyphs: distinct,
      ic,
      repeat_rate: rr,
      flat_random_baseline: distinct > 0 ? 1 / distinct : 0,
    };
  },
});

const sectionVerdict = defineTool({
  name: 'section_verdict',
  description:
    'Run Jakobsen SA against all three candidate languages (latin, hebrew, arabic) ' +
    'on a section of EVA text. Returns per-language results plus a single verdict ' +
    'string indicating whether monoalphabetic substitution is plausible.',
  parameters: z.object({
    eva_text: z.string(),
    iterations: z.number().int().positive().default(20_000).optional(),
    restarts: z.number().int().positive().default(4).optional(),
    seed: z.number().int().default(42).optional(),
  }),
  handler: async ({ eva_text, iterations, restarts, seed }) => {
    const tokens = tokenize(eva_text);
    const results = (['latin', 'hebrew', 'arabic'] as const).map((lang) =>
      runSA(tokens, MODELS[lang], iterations ?? 20_000, restarts ?? 4, 10.0, 0.01, seed ?? 42),
    );
    const best = results.reduce((a, b) => (a.perTokenScore > b.perTokenScore ? a : b));
    let verdict: string;
    if (best.perTokenScore > -3.5 && best.converged && best.repeatRate < 0.005) {
      verdict = `PLAUSIBLE — ${best.language} converged at per-token ${best.perTokenScore.toFixed(2)}`;
    } else if (best.perTokenScore > -4.5 && best.converged) {
      verdict = `WEAK — ${best.language} converged but score ${best.perTokenScore.toFixed(2)} is below natural-text range`;
    } else if (!best.converged) {
      verdict = `REJECTED — restarts diverged across all languages (best per-token ${best.perTokenScore.toFixed(2)}, ${best.language}); cipher unlikely to be monoalphabetic`;
    } else {
      verdict = `REJECTED — best per-token ${best.perTokenScore.toFixed(2)} far below natural-text range`;
    }
    return { results, verdict };
  },
});

// ---------------------------------------------------------------------------
// AppKit plugin wiring
// ---------------------------------------------------------------------------

const agentPlugin = createAgentPlugin({
  model: 'databricks-claude-sonnet-4-6',
  instructions: [
    'You are the Voynich Annealer — a monoalphabetic-substitution solver.',
    '',
    'When asked about a section of EVA text, call section_verdict with that text.',
    'Report the verdict verbatim. Do NOT speculate beyond what the SA result supports.',
    'If the verdict is REJECTED, explicitly recommend dropping monoalphabetic',
    'substitution from the search space for that section.',
  ].join('\n'),
  tools: [jakobsenSolve, computeIc, sectionVerdict],
});

const agentExports = () => agentPlugin.exports();

const app = express();
app.use(express.json());
agentPlugin.setup(app);

const discoveryPlugin = createDiscoveryPlugin(
  {
    name: 'voynich-annealer',
    description: "Jakobsen simulated-annealing solver for monoalphabetic substitution; rejects the substitution hypothesis when restarts don't converge.",
  },
  agentExports,
);
discoveryPlugin.setup();

const devPlugin = createDevPlugin({}, agentExports);

agentPlugin.injectRoutes(app);
discoveryPlugin.injectRoutes(app);
devPlugin.injectRoutes(app);

const port = parseInt(process.env.PORT ?? '8005');
app.listen(port, () => {
  console.log(`Voynich Annealer running at http://localhost:${port}`);
  console.log(`  POST /responses               — agent endpoint`);
  console.log(`  GET  /.well-known/agent.json  — A2A discovery card`);
  console.log(`  GET  /_apx/agent              — dev chat UI`);
  console.log(`  GET  /_apx/tools              — tool inspector`);
});
