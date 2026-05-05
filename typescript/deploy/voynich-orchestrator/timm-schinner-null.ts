// Timm-Schinner null model test.
//
// The Timm-Schinner (2004) self-citation model proposes the Voynich scribe
// generated text by repeatedly copying words from a sliding window of recently
// written text, with occasional single-character substitutions. This is known
// to reproduce Zipf's law mechanically.
//
// Question: does Timm-Schinner text also pass the FULL test battery from
// language-statistics.ts (Zipf, Heaps, conditional entropy, positional grammar)?
// If yes → our battery is not discriminating and the "real language" conclusion
//           is weaker than it appears.
// If no  → identify which tests the null model fails; those are genuine
//           discriminators, and our conclusions hold for those tests.
//
// Generation parameters (following Timm-Schinner 2004):
//   - Seed: first SEED_WORDS words of real EVA corpus (bootstraps the window)
//   - Window: look back W=7 positions in generated text
//   - Mutation: with probability P_MUT, replace one random character with
//     a random character from the EVA glyph set
//   - Target length: same token count as real corpus
//
// Run: npx tsx timm-schinner-null.ts

const ZL_URL = 'https://www.voynich.nu/data/ZL3b-n.txt';
const WINDOW = 7;
const P_MUT = 0.15;
const SEED_WORDS = 20;
const MIN_FREQ_ZIPF = 5;
const TOP_K_POSITIONAL = 100;
const N_HEAPS_POINTS = 20;

// EVA character set (the main alphabetic glyphs used in EVA transcriptions)
const EVA_CHARS = 'abcdefghijklmnopqrstuy'.split('');

// ── Parser (identical to language-statistics.ts) ──────────────────────────

function extractWords(line: string): string[] {
  let text = line.replace(/<[^>]+>/g, ' ');
  text = text.replace(/\{[^}]*\}/g, '').replace(/\[([^:\]]+):[^\]]*\]/g, '$1').replace(/@\d+;?/g, '').replace(/[!?%$]/g, '');
  return text.split(/[\s.,;-]+/).map(w => w.trim().toLowerCase()).filter(w => w.length >= 2 && /^[a-z]+$/.test(w));
}

interface ParsedLine { section: string; words: string[]; wordCount: number }

function parseCorpus(raw: string): { lines: ParsedLine[]; allWords: string[] } {
  const lines: ParsedLine[] = [];
  let section = '?';
  for (const lineRaw of raw.split('\n')) {
    const t = lineRaw.trim();
    if (!t || t.startsWith('#')) continue;
    const sm = t.match(/\$I=([A-Za-z])/);
    if (sm) section = sm[1].toUpperCase();
    if (/^<f\d+[rv]/.test(t) && t.includes('>')) {
      const words = extractWords(t);
      if (words.length > 0) lines.push({ section, words, wordCount: words.length });
    }
  }
  return { lines, allWords: lines.flatMap(l => l.words) };
}

// ── Math helpers (identical to language-statistics.ts) ────────────────────

function log2(x: number): number { return x <= 0 ? 0 : Math.log2(x); }

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

function erfc(x: number): number {
  if (x < 0) return 2 - erfc(-x);
  const t = 1 / (1 + 0.3275911 * x);
  const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
  return poly * Math.exp(-x * x);
}

function chiSquareP(chi2: number, df: number): number {
  const x = (chi2 / df) ** (1 / 3);
  const mu = 1 - 2 / (9 * df);
  const sigma = Math.sqrt(2 / (9 * df));
  const z = (x - mu) / sigma;
  return 0.5 * erfc(z / Math.SQRT2);
}

// ── Timm-Schinner generator ───────────────────────────────────────────────

function mutate(word: string): string {
  if (word.length === 0) return word;
  const pos = Math.floor(Math.random() * word.length);
  const ch = EVA_CHARS[Math.floor(Math.random() * EVA_CHARS.length)];
  return word.slice(0, pos) + ch + word.slice(pos + 1);
}

function generateTS(
  seed: string[],
  lineLengths: number[],
  targetTokens: number,
): { lines: Array<{ words: string[] }>; allWords: string[] } {
  // Circular buffer seeded with first SEED_WORDS real words
  const buf: string[] = seed.slice(0, SEED_WORDS);

  const generatedLines: Array<{ words: string[] }> = [];
  const allWords: string[] = [...buf];

  let lengthIdx = 0;
  let tokensGenerated = SEED_WORDS;

  while (tokensGenerated < targetTokens && lengthIdx < lineLengths.length) {
    const lineLen = lineLengths[lengthIdx % lineLengths.length];
    lengthIdx++;
    const lineWords: string[] = [];

    for (let i = 0; i < lineLen; i++) {
      // Sample from last WINDOW words in buffer
      const start = Math.max(0, buf.length - WINDOW);
      const idx = start + Math.floor(Math.random() * (buf.length - start));
      let word = buf[idx];

      // Occasional character substitution
      if (Math.random() < P_MUT) word = mutate(word);

      lineWords.push(word);
      buf.push(word);
      allWords.push(word);
      tokensGenerated++;
    }

    generatedLines.push({ words: lineWords });
  }

  return { lines: generatedLines, allWords };
}

// ── The four tests (adapted from language-statistics.ts) ─────────────────

function runTests(
  label: string,
  lines: Array<{ words: string[] }>,
  allWords: string[],
): {
  zipfAlpha: number; zipfR2: number;
  heapsBeta: number;
  entropyRatio: number;
  positionalFrac: number;
} {
  console.log(`\n${'═'.repeat(60)}`);
  console.log(`  ${label}`);
  console.log('═'.repeat(60));
  console.log(`  Tokens: ${allWords.length}   Lines: ${lines.length}`);

  const freq = new Map<string, number>();
  for (const w of allWords) freq.set(w, (freq.get(w) ?? 0) + 1);
  const ranked = [...freq.entries()].sort((a, b) => b[1] - a[1]);
  console.log(`  Types: ${freq.size}`);
  console.log(`  Top 5: ${ranked.slice(0, 5).map(([w, c]) => `${w}(${c})`).join('  ')}`);

  // ── Test 1: Zipf ────────────────────────────────────────────────────────
  const zipfData = ranked.filter(([, c]) => c >= MIN_FREQ_ZIPF);
  const logRanks = zipfData.map((_, i) => Math.log10(i + 1));
  const logFreqs = zipfData.map(([, c]) => Math.log10(c));
  const zipf = linreg(logRanks, logFreqs);
  const zipfAlpha = -zipf.slope;
  const zipfPass = zipfAlpha >= 0.8 && zipfAlpha <= 1.2 && zipf.r2 >= 0.95;
  console.log(`\n  Zipf: α=${zipfAlpha.toFixed(4)}  R²=${zipf.r2.toFixed(4)}  → ${zipfPass ? '✓ PASS' : '✗ FAIL'}  (target: α=0.8–1.2, R²>0.95)`);

  // ── Test 2: Heaps ───────────────────────────────────────────────────────
  const step = Math.max(1, Math.floor(allWords.length / N_HEAPS_POINTS));
  const heapsSeen = new Set<string>();
  const heapsPoints: Array<{ n: number; v: number }> = [];
  for (let i = 0; i < allWords.length; i++) {
    heapsSeen.add(allWords[i]);
    if ((i + 1) % step === 0 || i === allWords.length - 1) heapsPoints.push({ n: i + 1, v: heapsSeen.size });
  }
  const heapsX = heapsPoints.map(p => Math.log10(p.n));
  const heapsY = heapsPoints.map(p => Math.log10(p.v));
  const heaps = linreg(heapsX, heapsY);
  const heapsBeta = heaps.slope;
  const heapsPass = heapsBeta >= 0.40 && heapsBeta <= 0.65;
  console.log(`  Heaps: β=${heapsBeta.toFixed(4)}  R²=${heaps.r2.toFixed(4)}  → ${heapsPass ? '✓ PASS' : '✗ FAIL'}  (target: β=0.40–0.65)`);

  // ── Test 3: Conditional entropy ─────────────────────────────────────────
  const bigramCounts = new Map<string, number>();
  const bigramFrom   = new Map<string, number>();
  let bigramTotal = 0;
  for (const line of lines) {
    for (let i = 0; i < line.words.length - 1; i++) {
      const bg = `${line.words[i]}|${line.words[i + 1]}`;
      bigramCounts.set(bg, (bigramCounts.get(bg) ?? 0) + 1);
      bigramFrom.set(line.words[i], (bigramFrom.get(line.words[i]) ?? 0) + 1);
      bigramTotal++;
    }
  }
  const N = allWords.length;
  let H1 = 0;
  for (const [, c] of freq) H1 -= (c / N) * log2(c / N);
  let H2 = 0;
  for (const [bg, cnt] of bigramCounts) {
    const a = bg.split('|')[0];
    const pab = cnt / bigramTotal;
    const pBgivenA = cnt / (bigramFrom.get(a) ?? 1);
    H2 -= pab * log2(pBgivenA);
  }
  const ratio = H2 / H1;
  const entropyPass = ratio <= 0.88 && ratio >= 0.50;
  console.log(`  H₂/H₁: H₁=${H1.toFixed(4)}  H₂=${H2.toFixed(4)}  ratio=${ratio.toFixed(4)}  → ${entropyPass ? '✓ PASS' : '✗ FAIL'}  (target: 0.50–0.88)`);

  // ── Test 4: Positional grammar ───────────────────────────────────────────
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
  const totalInit = lines.reduce((a, l) => a + (l.words.length > 0 ? 1 : 0), 0);
  const totalMid  = allWords.length - totalInit * 2;
  const totalAll  = allWords.length;
  const pInit = totalInit / totalAll;
  const pFin  = totalInit / totalAll;
  const pMid  = totalMid  / totalAll;

  const topWords = ranked.slice(0, TOP_K_POSITIONAL).map(([w]) => w);
  let sigAny = 0;
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
    if (p < 0.01) sigAny++;
  }
  const positionalFrac = sigAny / TOP_K_POSITIONAL;
  const positionalPass = positionalFrac >= 0.30;
  console.log(`  Positional: ${sigAny}/${TOP_K_POSITIONAL} sig. (p<0.01) = ${(positionalFrac * 100).toFixed(0)}%  → ${positionalPass ? '✓ PASS' : '✗ FAIL'}  (target: ≥30%)`);

  return { zipfAlpha, zipfR2: zipf.r2, heapsBeta, entropyRatio: ratio, positionalFrac };
}

// ── Main ──────────────────────────────────────────────────────────────────

async function main() {
  console.log('Timm-Schinner null model test');
  console.log(`Source: ${ZL_URL}`);
  console.log(`Window: W=${WINDOW}  Mutation rate: ${P_MUT}  Seed: ${SEED_WORDS} words\n`);

  const raw = await fetch(ZL_URL).then(r => r.text());
  const { lines: realLines, allWords: realWords } = parseCorpus(raw);
  console.log(`Real corpus: ${realWords.length} tokens, ${realLines.length} lines`);

  // ── Real corpus baseline ─────────────────────────────────────────────────
  const realResults = runTests('REAL EVA CORPUS (baseline)', realLines, realWords);

  // ── Timm-Schinner generation ─────────────────────────────────────────────
  console.log('\n\nGenerating Timm-Schinner synthetic corpus...');
  const lineLengths = realLines.map(l => l.words.length);
  const { lines: synthLines, allWords: synthWords } = generateTS(realWords, lineLengths, realWords.length);
  console.log(`Synthetic corpus: ${synthWords.length} tokens, ${synthLines.length} lines`);

  const synthResults = runTests('TIMM-SCHINNER SYNTHETIC (null model)', synthLines, synthWords);

  // ── Side-by-side comparison ───────────────────────────────────────────────
  console.log('\n\n' + '═'.repeat(75));
  console.log('  DISCRIMINABILITY COMPARISON');
  console.log('═'.repeat(75));

  type Row = { test: string; real: string; synth: string; realPass: boolean; synthPass: boolean };
  const rows: Row[] = [
    {
      test: 'Zipf α (0.8–1.2)',
      real:  `${realResults.zipfAlpha.toFixed(3)}  R²=${realResults.zipfR2.toFixed(3)}`,
      synth: `${synthResults.zipfAlpha.toFixed(3)}  R²=${synthResults.zipfR2.toFixed(3)}`,
      realPass:  realResults.zipfAlpha >= 0.8 && realResults.zipfAlpha <= 1.2 && realResults.zipfR2 >= 0.95,
      synthPass: synthResults.zipfAlpha >= 0.8 && synthResults.zipfAlpha <= 1.2 && synthResults.zipfR2 >= 0.95,
    },
    {
      test: 'Heaps β (0.40–0.65)',
      real:  realResults.heapsBeta.toFixed(3),
      synth: synthResults.heapsBeta.toFixed(3),
      realPass:  realResults.heapsBeta >= 0.40 && realResults.heapsBeta <= 0.65,
      synthPass: synthResults.heapsBeta >= 0.40 && synthResults.heapsBeta <= 0.65,
    },
    {
      test: 'H₂/H₁ (0.50–0.88)',
      real:  realResults.entropyRatio.toFixed(3),
      synth: synthResults.entropyRatio.toFixed(3),
      realPass:  realResults.entropyRatio <= 0.88 && realResults.entropyRatio >= 0.50,
      synthPass: synthResults.entropyRatio <= 0.88 && synthResults.entropyRatio >= 0.50,
    },
    {
      test: 'Positional (≥30%)',
      real:  `${(realResults.positionalFrac * 100).toFixed(0)}%`,
      synth: `${(synthResults.positionalFrac * 100).toFixed(0)}%`,
      realPass:  realResults.positionalFrac >= 0.30,
      synthPass: synthResults.positionalFrac >= 0.30,
    },
  ];

  console.log('Test'.padEnd(24) + 'Real EVA'.padEnd(28) + 'TS Synthetic'.padEnd(28) + 'Discriminates?');
  console.log('─'.repeat(90));
  for (const r of rows) {
    const realStr  = (r.realPass  ? '✓ ' : '✗ ') + r.real;
    const synthStr = (r.synthPass ? '✓ ' : '✗ ') + r.synth;
    const discriminates = r.realPass && !r.synthPass ? 'YES ← genuine discriminator' :
                          !r.realPass && r.synthPass ? 'inverted (real fails, synth passes)' :
                          r.realPass && r.synthPass  ? 'no (both pass)' :
                                                       'no (both fail)';
    console.log(r.test.padEnd(24) + realStr.padEnd(28) + synthStr.padEnd(28) + discriminates);
  }

  const discriminators = rows.filter(r => r.realPass && !r.synthPass).length;
  const bothPass       = rows.filter(r => r.realPass && r.synthPass).length;

  console.log('\n' + '═'.repeat(75));
  console.log('  INTERPRETATION');
  console.log('═'.repeat(75));

  if (discriminators >= 2) {
    console.log(`\n${discriminators}/4 tests discriminate real EVA from Timm-Schinner synthetic text.`);
    console.log('The battery is NOT trivially fooled by the self-citation null model.');
    console.log('Tests that PASS for real EVA but FAIL for synthetic are genuine evidence');
    console.log('for structure that goes beyond mechanical self-citation.');
    console.log('\nConclusion: "real language" claim is supported by tests the null model fails.');
  } else if (bothPass >= 4) {
    console.log('\nAll 4 tests pass for BOTH real EVA and Timm-Schinner synthetic text.');
    console.log('WARNING: the battery is not discriminating.');
    console.log('The "real language" conclusion is NOT supported by these tests alone.');
    console.log('The Timm-Schinner model reproduces all four signatures mechanically.');
    console.log('\nConclusion: need additional tests to distinguish real language from self-citation.');
  } else {
    console.log(`\n${discriminators}/4 tests discriminate. The picture is mixed.`);
    console.log(`${bothPass} tests pass for both, ${discriminators} discriminate, ${rows.length - discriminators - bothPass} are inconclusive.`);
  }

  console.log('\nDone.');
}

main().catch(e => { console.error(e); process.exit(1); });
