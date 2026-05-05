// typescript/deploy/voynich-orchestrator/stat-types.ts

export interface HypothesisSpec {
  feature: string;        // e.g. "qo-prefix rate", "word entropy", "-dy suffix rate"
  method: string;         // "rank-biserial" | "permutation-test"
  family_a: string;       // e.g. "solanaceae"
  family_b: string;       // e.g. "all-botanical" | "thistle" | "plantago"
  folio_filter?: string;
  null_model?: string;    // "label-shuffle" | "within-quire-shuffle"
  rationale: string;
}

export interface StatFinding {
  spec: HypothesisSpec;
  effect_size: number;
  p_value: number;
  n_samples: number;
  result_table?: string;
  interpretation: string;
}

export const BOTANICAL_FAMILIES = new Set([
  'solanaceae', 'thistle', 'plantago', 'poppy', 'rose',
  'mint-family', 'ranunculaceae', 'apiaceae', 'verbena',
  'artemisia', 'brassicaceae', 'lily-family',
]);

export const FINDINGS_SUMMARY = `
Established findings (FINDINGS.md + grid-scan-v1, 2026-05-04):

1. Jaccard clustering: within-family lift = 1.10× overall (r=-0.003, no distance decay).
2. Morphological fingerprints — all permutation-tested (N≥1000), all p<0.05:
   Solanaceae (n=33) vs all-botanical:
     qo-prefix: r=+0.532 p<0.001 | -chy: r=-0.435 p<0.001 | -dy: r=+0.367 p<0.001
     ch-init: r=-0.249 p=0.017 | short (≤3): r=-0.300 p=0.015 | word entropy: r=+0.426 p=0.002
     long word rate: r=+0.254 p=0.042
   Thistle (n=30) vs all-botanical:
     qo-prefix: r=-0.340 p=0.013 | short word: r=+0.340 p=0.005 | unique word ratio: r=+0.297 p=0.015
     -ain suffix: r=-0.353 p=0.004
   Plantago (n=10) vs all-botanical:
     ch-init: r=+0.563 p<0.001 | -dy: r=-0.449 p=0.018 | -chy: r=+0.402 p=0.039 | ok-prefix: r=-0.465 p=0.015
   Poppy (n=7) vs all-botanical:
     -aiin suffix: r=+0.625 p=0.004 | long word rate: r=-0.440 p=0.044
   Lily-family (n=4) vs all-botanical:
     -ain suffix: r=+0.646 p=0.024 | sh-init: r=+0.604 p=0.043
   Artemisia (n=3) vs all-botanical:
     short word: r=-0.770 p=0.015 | ok-prefix: r=-0.700 p=0.038
3. Two-tier label/text structure: 13 significant label words (p<0.05, 2000-perm null).
4. LOO family attribution: 52.5% accuracy (4-class, chance=25%), lift=2.10×, p<0.0001.
5. Grid scan (gen-11, grid-scan-v1): 60 significant results / 420 tests (14.3% hit rate).

ANTI-CORRELATION TABLE (direct family-vs-family, confirmed):
  qo-prefix: solanaceae +0.532 vs all; thistle -0.340 vs all; direct: sol vs thistle r=+0.560***
  -dy/-chy: solanaceae ↑-dy ↓-chy; plantago ↓-dy ↑-chy; direct: sol vs plantago -chy r=-0.697***
  short word: thistle ↑; solanaceae ↓; artemisia ↓ (r=-0.770)
  long word: solanaceae ↑ (+0.254); poppy ↓ (-0.440)
  -ain suffix: lily ↑ (+0.646); thistle ↓ (-0.353)

WHAT HAS BEEN TESTED (do not re-test):
  All 15 features vs all 7 viable families (vs all-botanical AND all pairwise) — grid-scan-v1 is exhaustive.
  Only propose tests using a NEW feature not in the list OR a new family combination outside the 15×7 grid.

UNTESTED DIMENSIONS (explore these):
  - Rose family (n=2 — too small for permutation)
  - Ranunculaceae, apiaceae, verbena, brassicaceae (small n)
  - Folio-level: does -aiin WITHIN a folio anti-correlate with qo-prefix? (within-folio feature correlation)
  - Quire-level: cross-quire consistency of new features (poppy -aiin, lily -ain, artemisia short)
  - Species-level: within solanaceae, do Mandragora vs Atropa vs Hyoscyamus show different EVA patterns?

What failed: NPMI threshold-count permutation test (p=0.212).
`.trim();

export function extractEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

export function classifyPlantFamily(subjectCandidates: string): string {
  let candidates: Array<{ name: string; latin: string; confidence: number }> = [];
  try { candidates = JSON.parse(subjectCandidates); } catch { return 'unknown'; }
  const top = candidates[0];
  if (!top || top.confidence < 0.35) return 'uncertain';
  const latin = (top.latin ?? '').toLowerCase();
  const name  = (top.name ?? '').toLowerCase();
  if (/papaver|poppy/.test(latin + name)) return 'poppy';
  if (/rosa|rose/.test(latin + name)) return 'rose';
  if (/mentha|mint|melissa|salvia|sage|thymus|thyme|origanum|oregano|rosmarinus|lavandula/.test(latin + name)) return 'mint-family';
  if (/cirsium|carduus|thistle|carlina|centaurea/.test(latin + name)) return 'thistle';
  if (/helleborus|aconitum|ranunculus|clematis|anemone/.test(latin + name)) return 'ranunculaceae';
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus|datura/.test(latin + name)) return 'solanaceae';
  if (/plantago|plantain/.test(latin + name)) return 'plantago';
  if (/foeniculum|apium|petroselinum|coriandrum|anethum|daucus/.test(latin + name)) return 'apiaceae';
  if (/verbena/.test(latin + name)) return 'verbena';
  if (/artemisia|absinthium|wormwood/.test(latin + name)) return 'artemisia';
  if (/brassica|sinapis|mustard|cabbage/.test(latin + name)) return 'brassicaceae';
  if (/iris|lily|lilium|crocus/.test(latin + name)) return 'lily-family';
  return 'other-botanical';
}

/** Rank-biserial correlation — non-parametric effect size for two-group comparison. */
export function rankBiserial(groupA: number[], groupB: number[]): number {
  const nA = groupA.length, nB = groupB.length;
  if (nA === 0 || nB === 0) return 0;
  let u = 0;
  for (const a of groupA) {
    for (const b of groupB) {
      if (a > b) u++;
      else if (a === b) u += 0.5;
    }
  }
  return (2 * u) / (nA * nB) - 1;
}

export function shuffle<T>(arr: T[]): T[] {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

/** Compute a named EVA feature rate for a list of words. Returns null for unknown features. */
export function computeFeature(words: string[], feature: string): number | null {
  const n = words.length;
  if (n === 0) return null;
  switch (feature) {
    case 'qo-prefix rate':    return words.filter(w => w.startsWith('qo')).length / n;
    case '-chy suffix rate':  return words.filter(w => w.endsWith('chy')).length / n;
    case '-dy suffix rate':   return words.filter(w => w.endsWith('dy')).length / n;
    case 'ch-init rate':      return words.filter(w => w.startsWith('ch')).length / n;
    case 'short word rate':   return words.filter(w => w.length <= 3).length / n;
    case 'unique word ratio': return new Set(words).size / n;
    case 'word entropy': {
      const freq = new Map<string, number>();
      for (const w of words) freq.set(w, (freq.get(w) ?? 0) + 1);
      let entropy = 0;
      for (const c of freq.values()) {
        const p = c / n;
        entropy -= p * Math.log2(p);
      }
      return entropy;
    }
    case 'oq-prefix rate':    return words.filter(w => w.startsWith('oq')).length / n;
    case '-ol suffix rate':   return words.filter(w => w.endsWith('ol')).length / n;
    case '-ain suffix rate':  return words.filter(w => w.endsWith('ain')).length / n;
    case '-aiin suffix rate': return words.filter(w => w.endsWith('aiin')).length / n;
    case 'sh-init rate':      return words.filter(w => w.startsWith('sh')).length / n;
    case 'ok-prefix rate':    return words.filter(w => w.startsWith('ok')).length / n;
    case 'mean word length':  return words.reduce((s, w) => s + w.length, 0) / n;
    case 'long word rate':    return words.filter(w => w.length >= 6).length / n;
    default: return null;
  }
}

/** Normal approximation p-value for rank-biserial r (two-tailed). */
export function approxPValue(r: number, nA: number, nB: number): number {
  const variance = (nA + nB + 1) / (3 * nA * nB);
  const z = Math.abs(r) / Math.sqrt(variance);
  // Two-tailed normal p-value via complementary error function approximation
  const t = 1 / (1 + 0.2316419 * z);
  const poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  const pdf = Math.exp(-0.5 * z * z) / Math.sqrt(2 * Math.PI);
  const tailP = pdf * poly;
  return Math.min(1, 2 * tailP);
}
