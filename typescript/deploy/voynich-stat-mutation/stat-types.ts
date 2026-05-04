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
Prior work established four lines of evidence (FINDINGS.md, 2026-05-04):

1. Jaccard clustering: within-family lift = 1.10× overall (flat across distance bins, r=-0.003).
2. Morphological fingerprint (solanaceae only — permutation-tested):
   - qo-prefix: r=+0.532, p<0.001 (permutation max=0.415, N=1000)
   - -chy suffix: r=-0.435, p<0.001
   - -dy suffix: r=+0.367, p<0.001
   - ch-init: r=-0.249, p=0.017
   - short (≤3): r=-0.300, p=0.003
3. Two-tier label/text structure: 13 significant label words (once_rate≥0.70, p<0.05).
   5/13 confirmed botanical-specific (cross-section test): kchy, ckhey, qockhol, ty, oldaiin.
4. NPMI semantic coherence: qualitative only (p=0.212 for permutation test — method invalid).
   Control B: 1/30 cross-family vs 15/15 same-family NPMI associations.

What has NOT been permutation-tested: thistle and plantago morphological fingerprints.
What has NOT been tested: word entropy, unique-word ratio, bigram diversity per family.
What failed: NPMI threshold-count permutation test — needs a different statistical approach.
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
