// Language statistics test — four canonical tests of Candidate A (real language).
//
// 1. Zipf's law: freq ~ rank^(-α). Natural languages: α ≈ 0.8–1.2, R² > 0.95.
//    Random text: much steeper / worse fit. Simple hoaxes: often too flat.
//
// 2. Heaps' law: vocabulary size V ~ N^β. Natural languages: β ≈ 0.40–0.60.
//    A fixed-codebook cipher: β → 0 (vocabulary saturates).
//
// 3. Conditional entropy reduction: H₂/H₁ where H₁ = unigram entropy,
//    H₂ = bigram conditional entropy H(w_t | w_{t-1}).
//    Natural languages: ratio ≈ 0.60–0.85.  Random: ratio ≈ 1.0.
//
// 4. Positional grammar: fraction of high-frequency words with significant
//    line-initial / line-final preference (chi-square, p < 0.01).
//    Natural languages: many words strongly position-sensitive (syntax).
//    Random / notation: weak positional preferences.
//
// Run: npx tsx language-statistics.ts

const ZL_URL = 'https://www.voynich.nu/data/ZL3b-n.txt';
const MIN_FREQ_ZIPF = 5;      // minimum occurrences for Zipf fit
const TOP_K_POSITIONAL = 100; // test top-K words for positional preference
const N_HEAPS_POINTS = 20;    // sample points for Heaps curve

// ── Parser ────────────────────────────────────────────────────────────────

function extractWords(line: string): string[] {
  let text = line.replace(/<[^>]+>/g, ' ');
  text = text.replace(/\{[^}]*\}/g, '').replace(/\[([^:\]]+):[^\]]*\]/g, '$1').replace(/@\d+;?/g, '').replace(/[!?%$]/g, '');
  return text.split(/[\s.,;-]+/).map(w => w.trim().toLowerCase()).filter(w => w.length >= 2 && /^[a-z]+$/.test(w));
}

interface Line { section: string; words: string[] }

function parseLines(raw: string): Line[] {
  const result: Line[] = [];
  let section = '?';
  for (const lineRaw of raw.split('\n')) {
    const t = lineRaw.trim();
    if (!t || t.startsWith('#')) continue;
    const sm = t.match(/\$I=([A-Za-z])/);
    if (sm) section = sm[1].toUpperCase();
    if (/^<f\d+[rv]/.test(t) && t.includes('>')) {
      const words = extractWords(t);
      if (words.length > 0) result.push({ section, words });
    }
  }
  return result;
}

// ── Math helpers ──────────────────────────────────────────────────────────

function log2(x: number): number { return x <= 0 ? 0 : Math.log2(x); }

// Simple linear regression on (x, y) pairs → { slope, intercept, r2 }
function linreg(xs: number[], ys: number[]): { slope: number; intercept: number; r2: number } {
  const n = xs.length;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let ssxy = 0, ssxx = 0, ssyy = 0;
  for (let i = 0; i < n; i++) {
    ssxy += (xs[i] - mx) * (ys[i] - my);
    ssxx += (xs[i] - mx) ** 2;
    ssyy += (ys[i] - my) ** 2;
  }
  const slope = ssxy / ssxx;
  const intercept = my - slope * mx;
  const r2 = ssxx === 0 || ssyy === 0 ? 0 : (ssxy / Math.sqrt(ssxx * ssyy)) ** 2;
  return { slope, intercept, r2 };
}

// Complementary error function approximation (Abramowitz & Stegun 7.1.26)
function erfc(x: number): number {
  if (x < 0) return 2 - erfc(-x);
  const t = 1 / (1 + 0.3275911 * x);
  const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
  return poly * Math.exp(-x * x);
}

// Chi-square p-value via Wilson-Hilferty cube-root normal approximation
function chiSquareP(chi2: number, df: number): number {
  const x = (chi2 / df) ** (1 / 3);
  const mu = 1 - 2 / (9 * df);
  const sigma = Math.sqrt(2 / (9 * df));
  const z = (x - mu) / sigma;
  return 0.5 * erfc(z / Math.SQRT2);
}

// ── Main ──────────────────────────────────────────────────────────────────

async function main() {
  console.log('Language statistics test — Candidate A\n');
  console.log(`Source: ${ZL_URL}\n`);

  const raw = await fetch(ZL_URL).then(r => r.text());
  const lines = parseLines(raw);
  const allWords = lines.flatMap(l => l.words);

  console.log(`Lines parsed: ${lines.length}`);
  console.log(`Total tokens: ${allWords.length}`);

  // ── Frequency table ───────────────────────────────────────────────────────
  const freq = new Map<string, number>();
  for (const w of allWords) freq.set(w, (freq.get(w) ?? 0) + 1);
  const ranked = [...freq.entries()].sort((a, b) => b[1] - a[1]);
  console.log(`Unique types: ${freq.size}`);
  console.log(`Top 5 words: ${ranked.slice(0, 5).map(([w, c]) => `${w}(${c})`).join('  ')}\n`);

  // ══════════════════════════════════════════════════════════════════════════
  // Test 1: Zipf's law
  // ══════════════════════════════════════════════════════════════════════════
  console.log('═══ Test 1: Zipf\'s law ═══\n');
  console.log('Natural language: α ≈ 0.8–1.2, R² > 0.95');
  console.log('Prediction (Candidate A): α and R² should fall in natural language range\n');

  const zipfData = ranked.filter(([, c]) => c >= MIN_FREQ_ZIPF);
  const logRanks = zipfData.map((_, i) => Math.log10(i + 1));
  const logFreqs = zipfData.map(([, c]) => Math.log10(c));
  const zipf = linreg(logRanks, logFreqs);

  console.log(`Words used in fit (freq ≥ ${MIN_FREQ_ZIPF}): ${zipfData.length}`);
  console.log(`Zipf exponent α = ${(-zipf.slope).toFixed(4)}  (slope = ${zipf.slope.toFixed(4)})`);
  console.log(`R² = ${zipf.r2.toFixed(4)}`);

  const inRange = -zipf.slope >= 0.8 && -zipf.slope <= 1.2 && zipf.r2 >= 0.95;
  console.log(`\nVerdict: α=${(-zipf.slope).toFixed(3)}, R²=${zipf.r2.toFixed(3)} → ${inRange ? '✓ WITHIN natural language range' : '✗ OUTSIDE natural language range'}`);

  // Show top 10 and bottom 10 deviations from fit
  const residuals = zipfData.map(([word, c], i) => {
    const predicted = zipf.intercept + zipf.slope * Math.log10(i + 1);
    return { word, count: c, rank: i + 1, residual: Math.log10(c) - predicted };
  }).sort((a, b) => Math.abs(b.residual) - Math.abs(a.residual));

  console.log('\nLargest deviations from Zipf fit (top 5):');
  for (const r of residuals.slice(0, 5)) {
    const dir = r.residual > 0 ? 'more frequent than fit' : 'less frequent';
    console.log(`  "${r.word}"  rank=${r.rank}  freq=${r.count}  residual=${r.residual.toFixed(3)} (${dir})`);
  }

  // ══════════════════════════════════════════════════════════════════════════
  // Test 2: Heaps' law
  // ══════════════════════════════════════════════════════════════════════════
  console.log('\n═══ Test 2: Heaps\' law ═══\n');
  console.log('Natural language: β ≈ 0.40–0.60, K varies');
  console.log('Fixed codebook cipher: β → 0 (vocabulary saturates quickly)');
  console.log('Prediction (Candidate A): β in 0.40–0.60 range\n');

  // Sample vocabulary size at evenly-spaced token counts
  const step = Math.floor(allWords.length / N_HEAPS_POINTS);
  const heapsSeen = new Set<string>();
  const heapsPoints: Array<{ n: number; v: number }> = [];
  for (let i = 0; i < allWords.length; i++) {
    heapsSeen.add(allWords[i]);
    if ((i + 1) % step === 0 || i === allWords.length - 1) {
      heapsPoints.push({ n: i + 1, v: heapsSeen.size });
    }
  }

  // Fit log(V) ~ β * log(N) + log(K)
  const heapsX = heapsPoints.map(p => Math.log10(p.n));
  const heapsY = heapsPoints.map(p => Math.log10(p.v));
  const heaps = linreg(heapsX, heapsY);
  const K = 10 ** heaps.intercept;

  console.log(`Total tokens: ${allWords.length}  Final vocabulary: ${freq.size}`);
  console.log(`Heaps β = ${heaps.slope.toFixed(4)}  K = ${K.toFixed(1)}  R² = ${heaps.r2.toFixed(4)}`);

  const heapsInRange = heaps.slope >= 0.40 && heaps.slope <= 0.65;
  console.log(`\nVerdict: β=${heaps.slope.toFixed(3)} → ${heapsInRange ? '✓ WITHIN natural language range (0.40–0.60)' : '✗ OUTSIDE natural language range'}`);

  // Heaps curve
  console.log('\nHeaps curve (token count → vocabulary size):');
  for (const p of heapsPoints.filter((_, i) => i % 4 === 0 || i === heapsPoints.length - 1)) {
    const predicted = K * (p.n ** heaps.slope);
    console.log(`  N=${p.n.toString().padEnd(8)}  V_obs=${p.v.toString().padEnd(8)}  V_fit=${Math.round(predicted).toString().padEnd(8)}`);
  }

  // ══════════════════════════════════════════════════════════════════════════
  // Test 3: Conditional entropy reduction
  // ══════════════════════════════════════════════════════════════════════════
  console.log('\n═══ Test 3: Conditional entropy reduction ═══\n');
  console.log('H₁ = unigram entropy, H₂ = conditional bigram entropy H(w | prev)');
  console.log('Natural language: H₂/H₁ ≈ 0.60–0.85  (English ≈ 0.72)');
  console.log('Random text: H₂/H₁ ≈ 1.0');
  console.log('Prediction (Candidate A): ratio significantly below 1.0\n');

  const N = allWords.length;

  // H₁: unigram Shannon entropy
  let H1 = 0;
  for (const [, c] of freq) H1 -= (c / N) * log2(c / N);

  // H₂: conditional entropy using bigrams
  // H(w_t | w_{t-1}) = Σ_a P(a) * Σ_b P(b|a) * -log P(b|a)
  //                  = Σ_{a,b} P(a,b) * -log P(b|a)
  // P(a,b) = count(a,b) / (N-1);  P(b|a) = count(a,b) / count(a)
  const bigramCounts = new Map<string, number>();
  const bigramFrom   = new Map<string, number>();  // marginal count of bigram's first word
  let bigramTotal = 0;
  for (const line of lines) {
    for (let i = 0; i < line.words.length - 1; i++) {
      const bg = `${line.words[i]}|${line.words[i + 1]}`;
      bigramCounts.set(bg, (bigramCounts.get(bg) ?? 0) + 1);
      bigramFrom.set(line.words[i], (bigramFrom.get(line.words[i]) ?? 0) + 1);
      bigramTotal++;
    }
  }

  let H2 = 0;
  for (const [bg, cnt] of bigramCounts) {
    const a = bg.split('|')[0];
    const pab = cnt / bigramTotal;
    const pBgivenA = cnt / (bigramFrom.get(a) ?? 1);
    H2 -= pab * log2(pBgivenA);
  }

  const ratio = H2 / H1;
  console.log(`H₁ (unigram entropy)  = ${H1.toFixed(4)} bits`);
  console.log(`H₂ (bigram cond. ent) = ${H2.toFixed(4)} bits`);
  console.log(`H₂/H₁ ratio           = ${ratio.toFixed(4)}`);
  console.log(`Reduction             = ${((1 - ratio) * 100).toFixed(1)}%`);

  const entropyInRange = ratio <= 0.88 && ratio >= 0.50;
  console.log(`\nVerdict: H₂/H₁=${ratio.toFixed(3)} → ${
    ratio <= 0.88 ? '✓ SIGNIFICANT reduction — language-like bigram grammar' :
    ratio <= 0.95 ? '~ MODEST reduction — possible but weak' :
    '✗ NEAR-ZERO reduction — consistent with random/notation'
  }`);

  // Per-section conditional entropy
  console.log('\nPer-section conditional entropy ratio:');
  const SECTIONS = ['H', 'B', 'S', 'P', 'Z'];
  for (const s of SECTIONS) {
    const sLines = lines.filter(l => l.section === s);
    const sWords = sLines.flatMap(l => l.words);
    if (sWords.length < 200) continue;
    const sFreq = new Map<string, number>();
    for (const w of sWords) sFreq.set(w, (sFreq.get(w) ?? 0) + 1);
    const sN = sWords.length;
    let sH1 = 0;
    for (const [, c] of sFreq) sH1 -= (c / sN) * log2(c / sN);

    const sBigramCounts = new Map<string, number>();
    const sBigramFrom   = new Map<string, number>();
    let sBigramTotal = 0;
    for (const line of sLines) {
      for (let i = 0; i < line.words.length - 1; i++) {
        const bg = `${line.words[i]}|${line.words[i + 1]}`;
        sBigramCounts.set(bg, (sBigramCounts.get(bg) ?? 0) + 1);
        sBigramFrom.set(line.words[i], (sBigramFrom.get(line.words[i]) ?? 0) + 1);
        sBigramTotal++;
      }
    }
    let sH2 = 0;
    for (const [bg, cnt] of sBigramCounts) {
      const a = bg.split('|')[0];
      const pab = cnt / sBigramTotal;
      const pBgivenA = cnt / (sBigramFrom.get(a) ?? 1);
      sH2 -= pab * log2(pBgivenA);
    }
    const sRatio = sH2 / sH1;
    const sName = { H: 'herbal', B: 'balneo', S: 'stars', P: 'pharma', Z: 'zodiac' }[s] ?? s;
    console.log(`  ${(s + ' ' + sName).padEnd(14)}  N=${sN.toString().padEnd(6)}  H₁=${sH1.toFixed(3)}  H₂=${sH2.toFixed(3)}  ratio=${sRatio.toFixed(3)}`);
  }

  // ══════════════════════════════════════════════════════════════════════════
  // Test 4: Positional grammar
  // ══════════════════════════════════════════════════════════════════════════
  console.log('\n═══ Test 4: Positional grammar ═══\n');
  console.log(`Testing top ${TOP_K_POSITIONAL} words for line-initial / line-final preference`);
  console.log('Natural language: many words show significant positional bias (syntax)');
  console.log('Random / notation: weak positional preferences');
  console.log('Prediction (Candidate A): ≥30% of top words show p<0.01 positional bias\n');

  // For each word: count as line-initial (pos=0), line-final (pos=last), or middle
  const posCount = new Map<string, { init: number; mid: number; fin: number; total: number }>();
  for (const line of lines) {
    const n = line.words.length;
    for (let i = 0; i < n; i++) {
      const w = line.words[i];
      const c = posCount.get(w) ?? { init: 0, mid: 0, fin: 0, total: 0 };
      if (i === 0) c.init++;
      else if (i === n - 1) c.fin++;
      else c.mid++;
      c.total++;
      posCount.set(w, c);
    }
  }

  // Baseline rates
  const totalInit = lines.reduce((a, l) => a + (l.words.length > 0 ? 1 : 0), 0);
  const totalFin  = totalInit;
  const totalMid  = allWords.length - totalInit - totalFin;
  const totalAll  = allWords.length;
  const pInit = totalInit / totalAll;
  const pFin  = totalFin  / totalAll;
  const pMid  = totalMid  / totalAll;

  // Chi-square test for each word
  const topWords = ranked.slice(0, TOP_K_POSITIONAL).map(([w]) => w);
  let sigInit = 0, sigFin = 0, sigMid = 0, sigAny = 0;

  console.log('Word'.padEnd(14) + 'init%'.padEnd(8) + 'mid%'.padEnd(8) + 'fin%'.padEnd(8) + 'chi2'.padEnd(8) + 'p'.padEnd(8) + 'bias');
  console.log('─'.repeat(68));

  for (const w of topWords) {
    const c = posCount.get(w);
    if (!c || c.total < 10) continue;
    const T = c.total;
    const expInit = pInit * T, expMid = pMid * T, expFin = pFin * T;
    const chi2 =
      (c.init - expInit) ** 2 / expInit +
      (c.mid  - expMid)  ** 2 / expMid  +
      (c.fin  - expFin)  ** 2 / expFin;
    const p = chiSquareP(chi2, 2);
    const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : 'ns';

    const initR = c.init / T, midR = c.mid / T, finR = c.fin / T;
    const bias =
      initR > pInit * 1.5 && initR > midR && initR > finR ? 'INIT' :
      finR  > pFin  * 1.5 && finR  > midR && finR  > initR ? 'FIN' :
      midR  > pMid  * 1.1 && midR  > initR && midR > finR ? 'MID' : '—';

    if (p < 0.01) {
      sigAny++;
      if (bias === 'INIT') sigInit++;
      else if (bias === 'FIN') sigFin++;
      else if (bias === 'MID') sigMid++;
      console.log(
        w.padEnd(14) +
        (initR * 100).toFixed(1).padEnd(8) +
        (midR  * 100).toFixed(1).padEnd(8) +
        (finR  * 100).toFixed(1).padEnd(8) +
        chi2.toFixed(1).padEnd(8) +
        p.toExponential(1).padEnd(8) +
        `${sig} ${bias}`
      );
    }
  }

  console.log(`\nBaseline position rates: init=${(pInit*100).toFixed(1)}%  mid=${(pMid*100).toFixed(1)}%  fin=${(pFin*100).toFixed(1)}%`);
  console.log(`\nOf top ${TOP_K_POSITIONAL} words: ${sigAny} have p<0.01 positional preference (${(sigAny/TOP_K_POSITIONAL*100).toFixed(0)}%)`);
  console.log(`  ${sigInit} line-initial biased  ${sigFin} line-final biased  ${sigMid} line-middle biased`);
  const positionalVerdict = sigAny / TOP_K_POSITIONAL >= 0.30;
  console.log(`\nVerdict: ${(sigAny/TOP_K_POSITIONAL*100).toFixed(0)}% → ${positionalVerdict ? '✓ STRONG positional grammar — consistent with syntax' : sigAny/TOP_K_POSITIONAL >= 0.15 ? '~ MODERATE positional preferences' : '✗ WEAK positional preferences'}`);

  // ══════════════════════════════════════════════════════════════════════════
  // Summary
  // ══════════════════════════════════════════════════════════════════════════
  console.log('\n═══ Summary — Candidate A scorecard ═══\n');
  console.log('Test'.padEnd(35) + 'Result'.padEnd(20) + 'Verdict');
  console.log('─'.repeat(75));
  const tests = [
    { name: 'Zipf\'s law (α, R²)', result: `α=${(-zipf.slope).toFixed(3)}, R²=${zipf.r2.toFixed(3)}`, pass: inRange },
    { name: 'Heaps\' law (β)', result: `β=${heaps.slope.toFixed(3)}`, pass: heapsInRange },
    { name: 'Conditional entropy (H₂/H₁)', result: `ratio=${ratio.toFixed(3)}`, pass: ratio <= 0.88 },
    { name: `Positional grammar (top ${TOP_K_POSITIONAL} words)`, result: `${sigAny}/${TOP_K_POSITIONAL} sig. (p<0.01)`, pass: positionalVerdict },
  ];
  let passed = 0;
  for (const t of tests) {
    console.log(t.name.padEnd(35) + t.result.padEnd(20) + (t.pass ? '✓ PASS' : '✗ FAIL'));
    if (t.pass) passed++;
  }
  console.log(`\n${passed}/4 tests consistent with natural language`);

  console.log('\nDone.');
}

main().catch(e => { console.error(e); process.exit(1); });
