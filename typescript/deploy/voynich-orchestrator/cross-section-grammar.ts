// Cross-section grammar replication test.
// Key question: does the -dy/-chy within-folio anti-correlation persist
// in non-herbal sections (balneological, zodiac, pharmaceutical)?
//
// Candidate A prediction (real language): grammar should persist — the
//   -dy/-chy alternant is a morphological rule, not content annotation.
// Candidate C prediction (botanical notation): alternant should weaken or
//   disappear outside herbal context, where botanical semantics are absent.
//
// Also: morphological feature rates by section (do qo-prefix, -dy, -chy
//   mean rates differ across sections, or are they section-stable?).
// And: Jaccard unigram lift within balneological section (is there ANY
//   vocabulary clustering structure outside herbal, as a sanity check).
//
// Data source: Zandbergen-Landini ZL3b-n.txt (full manuscript, public).
//
// Run:
//   npx tsx cross-section-grammar.ts

const ZL_URL = 'https://www.voynich.nu/data/ZL3b-n.txt';
const N_PERMS = 1000;
const MIN_WORDS_PER_FOLIO = 20;
const MIN_FOLIOS_PER_SECTION = 8;

const SECTION_NAMES: Record<string, string> = {
  H: 'herbal', B: 'balneological', Z: 'zodiac', P: 'pharmaceutical',
  A: 'astrological', S: 'stars', C: 'cosmological', T: 'text/label',
};

// ── EVA parser (ordered word list, not set) ───────────────────────────────

function extractEvaWordList(line: string): string[] {
  let text = line.replace(/<[^>]+>/g, ' ');
  text = text
    .replace(/\{[^}]*\}/g, '')
    .replace(/\[([^:\]]+):[^\]]*\]/g, '$1')
    .replace(/@\d+;?/g, '')
    .replace(/[!?%$]/g, '');
  return text
    .split(/[\s.,;-]+/)
    .map(w => w.trim().toLowerCase())
    .filter(w => w.length >= 2 && /^[a-z]+$/.test(w));
}

function parseSectionFromMeta(line: string): string {
  const m = line.match(/\$I=([A-Za-z])/);
  return m ? m[1].toUpperCase() : '?';
}

interface Folio { id: string; section: string; words: string[] }

function parseTranscription(text: string): Folio[] {
  const lines = text.split('\n');
  const folios: Folio[] = [];
  let current: { id: string; section: string; words: string[] } | null = null;

  const flush = () => {
    if (current && current.words.length >= MIN_WORDS_PER_FOLIO) folios.push(current);
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;

    const folioMatch = trimmed.match(/^<(f\d+[rv]\d*)>/);
    if (folioMatch) {
      flush();
      current = { id: folioMatch[1], section: parseSectionFromMeta(trimmed), words: [] };
      continue;
    }

    if (current && /^<f\d+[rv]/.test(trimmed) && trimmed.includes('>')) {
      current.words.push(...extractEvaWordList(trimmed));
    }
  }
  flush();
  return folios;
}

// ── Feature computation ───────────────────────────────────────────────────

type Feature = '-dy rate' | '-chy rate' | 'qo-prefix rate' | 'ch-init rate' | 'short word rate';

function computeFeat(words: string[], feat: Feature): number {
  const n = words.length;
  switch (feat) {
    case '-dy rate':        return words.filter(w => w.endsWith('dy')).length / n;
    case '-chy rate':       return words.filter(w => w.endsWith('chy')).length / n;
    case 'qo-prefix rate':  return words.filter(w => w.startsWith('qo')).length / n;
    case 'ch-init rate':    return words.filter(w => w.startsWith('ch')).length / n;
    case 'short word rate': return words.filter(w => w.length <= 3).length / n;
  }
}

// ── Statistics ────────────────────────────────────────────────────────────

function pearsonR(xs: number[], ys: number[]): number {
  const n = xs.length;
  if (n < 3) return 0;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let num = 0, dx2 = 0, dy2 = 0;
  for (let i = 0; i < n; i++) {
    const dx = xs[i] - mx, dy = ys[i] - my;
    num += dx * dy; dx2 += dx * dx; dy2 += dy * dy;
  }
  const denom = Math.sqrt(dx2 * dy2);
  return denom === 0 ? 0 : num / denom;
}

function mean(xs: number[]): number { return xs.reduce((a, b) => a + b, 0) / xs.length; }

// Within-folio half-split correlation with permutation test.
// For each folio: split at midpoint, compute feature contrast (half1 - half2).
// Test whether contrast_A correlates with contrast_B across folios.
function withinFolioCorr(
  folios: Folio[],
  featA: Feature,
  featB: Feature,
): { r: number; p: number; n: number } {
  const xs: number[] = [], ys: number[] = [];
  const halves: Array<[string[], string[]]> = [];

  for (const f of folios) {
    const mid = Math.floor(f.words.length / 2);
    const h1 = f.words.slice(0, mid);
    const h2 = f.words.slice(mid);
    if (h1.length < 5 || h2.length < 5) continue;
    xs.push(computeFeat(h1, featA) - computeFeat(h2, featA));
    ys.push(computeFeat(h1, featB) - computeFeat(h2, featB));
    halves.push([h1, h2]);
  }

  if (xs.length < 8) return { r: NaN, p: NaN, n: xs.length };
  const r = pearsonR(xs, ys);

  let extreme = 0;
  for (let p = 0; p < N_PERMS; p++) {
    const pxs: number[] = [], pys: number[] = [];
    for (const [h1, h2] of halves) {
      const [a, b] = Math.random() < 0.5 ? [h1, h2] : [h2, h1];
      pxs.push(computeFeat(a, featA) - computeFeat(b, featA));
      pys.push(computeFeat(a, featB) - computeFeat(b, featB));
    }
    if (Math.abs(pearsonR(pxs, pys)) >= Math.abs(r)) extreme++;
  }

  return { r, p: extreme / N_PERMS, n: xs.length };
}

// Jaccard similarity
function jaccard(a: string[], b: string[]): number {
  const sa = new Set(a), sb = new Set(b);
  let inter = 0;
  for (const x of sa) if (sb.has(x)) inter++;
  return inter / (sa.size + sb.size - inter);
}

// ── Main ──────────────────────────────────────────────────────────────────

async function main() {
  console.log('Cross-section grammar replication test');
  console.log(`Source: ${ZL_URL}\n`);

  const res = await fetch(ZL_URL);
  if (!res.ok) throw new Error(`Download failed: ${res.status}`);
  const raw = await res.text();
  console.log(`Downloaded ${(raw.length / 1024).toFixed(0)} KB`);

  const folios = parseTranscription(raw);
  console.log(`Parsed ${folios.length} folios with ≥${MIN_WORDS_PER_FOLIO} words\n`);

  // Group by section
  const bySection = new Map<string, Folio[]>();
  for (const f of folios) {
    const arr = bySection.get(f.section) ?? [];
    arr.push(f);
    bySection.set(f.section, arr);
  }

  console.log('Section inventory:');
  const sections = [...bySection.keys()].sort();
  for (const s of sections) {
    const arr = bySection.get(s)!;
    const totalWords = arr.reduce((a, f) => a + f.words.length, 0);
    console.log(`  ${s}  ${(SECTION_NAMES[s] ?? s).padEnd(16)}  ${arr.length} folios  ${totalWords} words`);
  }

  // ── Part 1: Mean feature rates by section ────────────────────────────────
  console.log('\n═══ Part 1: Feature rates by section ═══\n');
  const features: Feature[] = ['-dy rate', '-chy rate', 'qo-prefix rate', 'ch-init rate', 'short word rate'];
  const eligible = sections.filter(s => (bySection.get(s)?.length ?? 0) >= MIN_FOLIOS_PER_SECTION);

  console.log('Section'.padEnd(20) + 'n'.padEnd(5) + features.map(f => f.slice(0, 8).padEnd(10)).join(''));
  console.log('─'.repeat(20 + 5 + features.length * 10));
  for (const s of eligible) {
    const arr = bySection.get(s)!;
    const rates = features.map(feat => {
      const vals = arr.map(f => computeFeat(f.words, feat));
      return mean(vals);
    });
    const name = `${s} ${SECTION_NAMES[s] ?? s}`;
    console.log(name.padEnd(20) + arr.length.toString().padEnd(5) + rates.map(r => r.toFixed(4).padEnd(10)).join(''));
  }

  // ── Part 2: Within-folio -dy/-chy anti-correlation by section ────────────
  console.log('\n═══ Part 2: Within-folio -dy vs -chy anti-correlation by section ═══\n');
  console.log('Herbal result (from previous analysis): r=−0.049, p<0.001*** n=224\n');
  console.log('Prediction: if EVA has morphological grammar, r should be negative in ALL sections.');
  console.log('            If herbal-only notation, r should be near 0 in non-herbal sections.\n');

  const PAIRS: Array<{ a: Feature; b: Feature; pred: string }> = [
    { a: '-dy rate', b: '-chy rate', pred: '−  (alternants)' },
    { a: 'qo-prefix rate', b: '-chy rate', pred: '−  (sol pattern)' },
    { a: 'qo-prefix rate', b: '-dy rate',  pred: '+  (co-occur sol)' },
  ];

  for (const { a, b, pred } of PAIRS) {
    console.log(`--- ${a} vs ${b} (prediction: ${pred}) ---`);
    console.log('Section'.padEnd(20) + 'n'.padEnd(5) + 'r'.padEnd(9) + 'p'.padEnd(10) + 'sig');
    console.log('─'.repeat(50));

    const herbalFolios = bySection.get('H') ?? [];
    const herbalR = withinFolioCorr(herbalFolios, a, b);
    const hSig = herbalR.p < 0.001 ? '***' : herbalR.p < 0.01 ? '**' : herbalR.p < 0.05 ? '*' : herbalR.p < 0.1 ? '~' : 'ns';
    console.log(`H herbal            ${herbalR.n.toString().padEnd(5)}${herbalR.r.toFixed(4).padEnd(9)}${herbalR.p.toFixed(4).padEnd(10)}${hSig}  ← baseline`);

    for (const s of eligible.filter(x => x !== 'H')) {
      const arr = bySection.get(s)!;
      const result = withinFolioCorr(arr, a, b);
      if (result.n < 8) {
        console.log(`${(s + ' ' + (SECTION_NAMES[s] ?? s)).padEnd(20)}${'—'.padEnd(5)}too few valid folios (${result.n})`);
        continue;
      }
      const sig = isNaN(result.p) ? '—' : result.p < 0.001 ? '***' : result.p < 0.01 ? '**' : result.p < 0.05 ? '*' : result.p < 0.1 ? '~' : 'ns';
      const rStr = isNaN(result.r) ? '—' : (result.r >= 0 ? '+' : '') + result.r.toFixed(4);
      const pStr = isNaN(result.p) ? '—' : result.p.toFixed(4);
      console.log(`${(s + ' ' + (SECTION_NAMES[s] ?? s)).padEnd(20)}${result.n.toString().padEnd(5)}${rStr.padEnd(9)}${pStr.padEnd(10)}${sig}`);
    }
    console.log();
  }

  // ── Part 3: Bigram n-gram lift in balneological and zodiac sections ───────
  console.log('\n═══ Part 3: Bigram vocabulary lift in non-herbal sections ═══\n');
  console.log('(Herbal bigram lift = 1.277×, p=0.038*. If grammar is section-general,');
  console.log(' non-herbal sections should also show some positive bigram lift.)\n');
  console.log('Note: no family labels in non-herbal — testing raw vocabulary clustering\n');
  console.log('Method: for each section, split folios into two arbitrary groups (odd/even index),');
  console.log('compute Jaccard overlap. This is NOT a family lift — just checks if folios within');
  console.log('the section share more bigrams than a shuffled null.\n');

  // For non-herbal, no family labels. Instead test: unigram Jaccard mean (how similar are folios
  // to each other within-section vs cross-section), vs a shuffled null.
  // This tests whether there's ANY vocabulary structure (clustering) beyond chance.

  const nonHerbalSections = eligible.filter(s => s !== 'H');
  const allFoliosPool = folios.filter(f => eligible.includes(f.section));

  function unigramJaccard(f1: Folio, f2: Folio): number {
    return jaccard(f1.words, f2.words);
  }

  function meanJaccard(group1: Folio[], group2?: Folio[]): number {
    const g2 = group2 ?? group1;
    let sum = 0, count = 0;
    for (let i = 0; i < group1.length; i++) {
      const jStart = group2 ? 0 : i + 1;
      for (let j = jStart; j < g2.length; j++) {
        if (!group2 && i === j) continue;
        sum += unigramJaccard(group1[i], g2[j]);
        count++;
      }
    }
    return count > 0 ? sum / count : 0;
  }

  for (const s of ['B', 'Z', 'P', 'S'].filter(s => eligible.includes(s))) {
    const arr = bySection.get(s)!;
    if (arr.length < 6) continue;

    const observed = meanJaccard(arr);

    // Permutation null: draw same-sized random sample from all eligible folios
    let extreme = 0;
    for (let p = 0; p < 500; p++) {
      const shuffled = [...allFoliosPool].sort(() => Math.random() - 0.5).slice(0, arr.length);
      if (meanJaccard(shuffled) >= observed) extreme++;
    }
    const pv = extreme / 500;
    const sig = pv < 0.001 ? '***' : pv < 0.01 ? '**' : pv < 0.05 ? '*' : pv < 0.1 ? '~' : 'ns';
    const name = `${s} ${SECTION_NAMES[s] ?? s}`;
    console.log(`${name.padEnd(22)}  n=${arr.length}  mean_J=${observed.toFixed(5)}  p=${pv.toFixed(3)} ${sig}`);
  }

  const herbalArr = bySection.get('H') ?? [];
  const herbalJ = meanJaccard(herbalArr);
  let herbalExtreme = 0;
  for (let p = 0; p < 500; p++) {
    const shuffled = [...allFoliosPool].sort(() => Math.random() - 0.5).slice(0, herbalArr.length);
    if (meanJaccard(shuffled) >= herbalJ) herbalExtreme++;
  }
  console.log(`H herbal (baseline)     n=${herbalArr.length}  mean_J=${herbalJ.toFixed(5)}  p=${(herbalExtreme/500).toFixed(3)} ${herbalExtreme/500 < 0.05 ? '*' : 'ns'}`);

  // ── Summary ───────────────────────────────────────────────────────────────
  console.log('\n═══ Summary ═══\n');
  console.log('If -dy/-chy anti-correlation holds in B (balneological) and Z (zodiac):');
  console.log('  → The grammar is section-general = supports Candidate A (real language)');
  console.log('If it disappears or reverses:');
  console.log('  → Grammar is herbal-specific = supports Candidate C (botanical notation)');

  console.log('\nDone.');
}

main().catch(e => { console.error(e); process.exit(1); });
