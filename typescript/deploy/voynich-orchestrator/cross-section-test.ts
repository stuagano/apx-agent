/**
 * Cross-section specificity test for label words.
 *
 * Downloads the Zandbergen-Landini EVA transcription (ZL3b-n.txt, full
 * manuscript), parses section labels from folio metadata ($I= parameter),
 * and compares per-folio occurrence rates for the 13 confirmed label words
 * vs. running-text words across all sections.
 *
 * If label words are plant-name identifiers or herbal-specific vocabulary,
 * they should be rare or absent in non-herbal sections (zodiac, balneological,
 * pharmaceutical, astronomical, stars). Running-text words should be
 * section-diffuse.
 *
 * Section codes in ZL file ($I parameter):
 *   H = herbal         B = balneological
 *   Z = zodiac         P = pharmaceutical
 *   A = astrological   S = stars (text-only pages)
 *   C = cosmological   T = text (label/key pages)
 *
 * Run:
 *   npx tsx cross-section-test.ts
 *   (no Databricks credentials needed — downloads public transcription file)
 */

const ZL_URL = 'https://www.voynich.nu/data/ZL3b-n.txt';

const LABEL_WORDS = [
  // solanaceae (p<0.05 from label-word-test.ts)
  'olaiin', 'qokeody', 'qockhey', 'ckhey', 'sy', 'oldaiin', 'cphey', 'okor', 'qockhol', 'qokan',
  // thistle (p<0.05)
  'kchy', 'ydaiin', 'ty',
];

const RUNNING_TEXT_WORDS = [
  'daiin', 'aiin', 'chol', 'shedy', 'qotaiin', 'lchedy', 'qokeedy',
];

// -------------------------------------------------------------------------
// Parse ZL ivtff format
// -------------------------------------------------------------------------

interface FolioData {
  folioId: string;
  section: string;
  words: Set<string>;
}

function parseSectionFromMeta(metaLine: string): string {
  const m = metaLine.match(/\$I=([A-Za-z])/);
  return m ? m[1].toUpperCase() : '?';
}

function extractEvaWords(line: string): string[] {
  // Remove line marker: <f1r.1,@P0>
  let text = line.replace(/<[^>]+>/g, ' ');
  // Remove special tokens: <%> <$> {variants} [alt:alt] @NNN; uncertain markers
  text = text
    .replace(/\{[^}]*\}/g, '')           // {uncertain}
    .replace(/\[([^:\]]+):[^\]]*\]/g, '$1')  // [word:alt] → keep first
    .replace(/@\d+;?/g, '')              // @NNN reference markers
    .replace(/[!?%$]/g, '');             // remaining specials
  return text
    .split(/[\s.,;]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

function parseTranscription(text: string): FolioData[] {
  const lines = text.split('\n');
  const folios: FolioData[] = [];

  let currentFolio: string | null = null;
  let currentSection = '?';
  let currentWords: string[] = [];

  const flush = () => {
    if (currentFolio && currentWords.length > 0) {
      folios.push({
        folioId: currentFolio,
        section: currentSection,
        words: new Set(currentWords),
      });
    }
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;

    // Folio header: <f1r>  <! $I=H ...>
    // Sometimes split across two tokens on the same line
    const folioMatch = trimmed.match(/^<(f\d+[rv]\d*)>/);
    if (folioMatch) {
      flush();
      currentFolio = folioMatch[1];
      currentSection = parseSectionFromMeta(trimmed);
      currentWords = [];
      continue;
    }

    // Text line: <f1r.1,@P0>  text...
    if (/^<f\d+[rv]/.test(trimmed) && trimmed.includes('>')) {
      currentWords.push(...extractEvaWords(trimmed));
    }
  }
  flush();
  return folios;
}

// -------------------------------------------------------------------------
// Main
// -------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('cross-section-test — label word rates across all manuscript sections');
  console.log(`Downloading ${ZL_URL} ...`);
  console.log('');

  const res = await fetch(ZL_URL);
  if (!res.ok) throw new Error(`Failed to download ZL file: ${res.status} ${res.statusText}`);
  const text = await res.text();
  console.log(`  Downloaded ${(text.length / 1024).toFixed(0)} KB`);

  const folios = parseTranscription(text);
  console.log(`  Parsed ${folios.length} folios`);
  console.log('');

  // Section distribution
  const sectionFolios = new Map<string, FolioData[]>();
  for (const f of folios) {
    const s = sectionFolios.get(f.section) ?? [];
    s.push(f);
    sectionFolios.set(f.section, s);
  }

  const SECTION_NAMES: Record<string, string> = {
    H: 'herbal', B: 'balneological', Z: 'zodiac', P: 'pharmaceutical',
    A: 'astrological', S: 'stars', C: 'cosmological', T: 'text/label', '?': 'unknown',
  };

  console.log('Section distribution:');
  const sections = [...sectionFolios.keys()].sort();
  for (const s of sections) {
    const name = SECTION_NAMES[s] ?? s;
    console.log(`  ${s}  (${name.padEnd(16)})  ${sectionFolios.get(s)!.length} folios`);
  }
  console.log('');

  // Per-word, per-section occurrence rate
  function rate(word: string, sectionCode: string): number {
    const sf = sectionFolios.get(sectionCode);
    if (!sf || sf.length === 0) return NaN;
    return sf.filter((f) => f.words.has(word)).length / sf.length;
  }

  // Build comparison: herbal rate vs. each non-herbal section
  const herbalCode = 'H';
  const nonHerbalCodes = sections.filter((s) => s !== herbalCode && s !== '?');

  // -------------------------------------------------------------------------
  // Full rate table
  // -------------------------------------------------------------------------
  console.log('=== OCCURRENCE RATE (fraction of folios with word) ===');
  console.log('');

  const colW = 9;
  const sectionHeaders = [herbalCode, ...nonHerbalCodes]
    .map((s) => (SECTION_NAMES[s] ?? s).slice(0, colW - 1).padEnd(colW))
    .join('');
  console.log('Word'.padEnd(16) + 'Type'.padEnd(8) + sectionHeaders + 'Specificity');
  console.log('-'.repeat(16 + 8 + colW * (nonHerbalCodes.length + 1) + 14));

  const ALL_WORDS = [...LABEL_WORDS, ...RUNNING_TEXT_WORDS];
  const results: Array<{ word: string; isLabel: boolean; herbalRate: number; nonHerbalRates: number[]; ratio: number }> = [];

  for (const word of ALL_WORDS) {
    const isLabel = LABEL_WORDS.includes(word);
    const hr = rate(word, herbalCode);
    const nonHerbalRates = nonHerbalCodes.map((s) => rate(word, s));
    const validNH = nonHerbalRates.filter((r) => !isNaN(r));
    const meanNH = validNH.length > 0 ? validNH.reduce((a, b) => a + b, 0) / validNH.length : 0;
    const ratio = meanNH === 0 ? Infinity : hr / meanNH;

    results.push({ word, isLabel, herbalRate: hr, nonHerbalRates, ratio });

    const allCodes = [herbalCode, ...nonHerbalCodes];
    const rateCols = allCodes.map((s) => {
      const r = rate(word, s);
      return (isNaN(r) ? '—' : r === 0 ? '0' : r.toFixed(2)).padEnd(colW);
    }).join('');
    const ratioStr = isFinite(ratio) ? `${ratio.toFixed(1)}×` : 'herbal-only';

    console.log(word.padEnd(16) + (isLabel ? 'label' : 'text').padEnd(8) + rateCols + ratioStr);
  }

  // -------------------------------------------------------------------------
  // Specificity summary
  // -------------------------------------------------------------------------
  console.log('');
  console.log('=== HERBAL SPECIFICITY RATIO (herbal_rate / mean_non-herbal_rate) ===');
  console.log('');

  const labelResults = results.filter((r) => r.isLabel).sort((a, b) => b.ratio - a.ratio);
  const textResults  = results.filter((r) => !r.isLabel).sort((a, b) => b.ratio - a.ratio);

  console.log('Label words:');
  console.log('Word'.padEnd(16) + 'Herbal'.padEnd(10) + 'Specificity');
  console.log('-'.repeat(40));
  for (const r of labelResults) {
    const ratioStr = isFinite(r.ratio) ? `${r.ratio.toFixed(1)}×` : 'herbal-only';
    console.log(r.word.padEnd(16) + r.herbalRate.toFixed(3).padEnd(10) + ratioStr);
  }

  console.log('');
  console.log('Running-text words (contrast):');
  console.log('Word'.padEnd(16) + 'Herbal'.padEnd(10) + 'Specificity');
  console.log('-'.repeat(40));
  for (const r of textResults) {
    const ratioStr = isFinite(r.ratio) ? `${r.ratio.toFixed(1)}×` : 'herbal-only';
    console.log(r.word.padEnd(16) + r.herbalRate.toFixed(3).padEnd(10) + ratioStr);
  }

  // -------------------------------------------------------------------------
  // Stats
  // -------------------------------------------------------------------------
  const labelHerbalOnly   = labelResults.filter((r) => !isFinite(r.ratio));
  const labelHighSpec     = labelResults.filter((r) => isFinite(r.ratio) && r.ratio >= 3);
  const textHerbalOnly    = textResults.filter((r)  => !isFinite(r.ratio));
  const textHighSpec      = textResults.filter((r)  => isFinite(r.ratio) && r.ratio >= 3);

  const finiteLabel = labelResults.filter((r) => isFinite(r.ratio)).map((r) => r.ratio);
  const finiteText  = textResults.filter((r) => isFinite(r.ratio)).map((r) => r.ratio);
  const median = (a: number[]) => a.length ? [...a].sort((x, y) => x - y)[Math.floor(a.length / 2)] : NaN;

  console.log('');
  console.log(`Label words: ${labelHerbalOnly.length}/${LABEL_WORDS.length} herbal-only, ` +
    `${labelHighSpec.length}/${LABEL_WORDS.length} specificity ≥3×`);
  console.log(`Text words:  ${textHerbalOnly.length}/${RUNNING_TEXT_WORDS.length} herbal-only, ` +
    `${textHighSpec.length}/${RUNNING_TEXT_WORDS.length} specificity ≥3×`);
  console.log(`Median specificity — label: ${isNaN(median(finiteLabel)) ? 'all-herbal-only' : median(finiteLabel).toFixed(1) + '×'}` +
    `  text: ${isNaN(median(finiteText)) ? 'all-herbal-only' : median(finiteText).toFixed(1) + '×'}`);
  console.log('');

  // -------------------------------------------------------------------------
  // Combined botanical (H+P) vs. non-botanical comparison
  // -------------------------------------------------------------------------
  // Note: our Databricks corpus classifies ALL botanical folios as "herbal",
  // but the ZL transcription separates herbal-A (f1-f57, $I=H) from
  // herbal-B / pharmaceutical (f87-f102, $I=P). They contain the same
  // botanical content. The correct comparison is H+P ("botanical") vs.
  // the clearly non-botanical sections (B=balneological, Z=zodiac, S=stars,
  // A=astrological, C=cosmological).
  console.log('');
  console.log('=== BOTANICAL (H+P) vs. NON-BOTANICAL (B+Z+S+A+C) ===');
  console.log('(accounts for ZL section-classification mismatch with our DB)');
  console.log('');

  const botanicalCodes    = ['H', 'P'];
  const nonBotanicalCodes = ['B', 'Z', 'S', 'A', 'C'];

  function pooledRate(word: string, codes: string[]): number {
    let withWord = 0, total = 0;
    for (const s of codes) {
      const sf = sectionFolios.get(s);
      if (!sf) continue;
      withWord += sf.filter((f) => f.words.has(word)).length;
      total += sf.length;
    }
    return total === 0 ? NaN : withWord / total;
  }

  console.log('Word'.padEnd(16) + 'Type'.padEnd(8) + 'Bot (H+P)'.padEnd(14) + 'Non-bot'.padEnd(14) + 'Ratio');
  console.log('-'.repeat(58));
  for (const word of ALL_WORDS) {
    const isLabel = LABEL_WORDS.includes(word);
    const botRate = pooledRate(word, botanicalCodes);
    const nhRate  = pooledRate(word, nonBotanicalCodes);
    const ratio   = nhRate === 0 ? Infinity : botRate / nhRate;
    const ratioStr = isFinite(ratio) ? `${ratio.toFixed(1)}×` : 'botanical-only';
    console.log(
      word.padEnd(16) +
      (isLabel ? 'label' : 'text').padEnd(8) +
      (isNaN(botRate) ? '—' : botRate.toFixed(3)).padEnd(14) +
      (isNaN(nhRate)  ? '—' : nhRate.toFixed(3)).padEnd(14) +
      ratioStr,
    );
  }

  // -------------------------------------------------------------------------
  // Verdict
  // -------------------------------------------------------------------------
  const totalLabelSpecific = labelHerbalOnly.length + labelHighSpec.length;
  const totalTextSpecific  = textHerbalOnly.length + textHighSpec.length;

  console.log('=== VERDICT ===');
  console.log('');

  if (totalLabelSpecific >= Math.ceil(LABEL_WORDS.length * 0.7) &&
      totalTextSpecific  <  Math.ceil(RUNNING_TEXT_WORDS.length * 0.5)) {
    console.log('Label words ARE herbal-specific; running-text words are NOT.');
    console.log('  The two tiers have different section profiles:');
    console.log('  → label tier: concentrated in the herbal section');
    console.log('  → text tier: present across manuscript sections');
    console.log('  This directly supports the label-as-plant-identifier hypothesis.');
    console.log('  Grammatical/function word explanation for the label tier is ruled out');
    console.log('  (function words appear everywhere; these do not).');
  } else if (totalLabelSpecific >= Math.ceil(LABEL_WORDS.length * 0.5)) {
    console.log('Label words are MOSTLY herbal-specific, running-text words somewhat diffuse.');
    console.log('  Mixed result. Some label words may be herbal plant identifiers;');
    console.log('  others may be herbal-dialect grammatical words rather than plant names.');
  } else if (totalTextSpecific >= Math.ceil(RUNNING_TEXT_WORDS.length * 0.7)) {
    console.log('Both label AND running-text words are herbal-specific.');
    console.log('  The ZL file may not contain sufficient non-herbal transcription data,');
    console.log('  or the herbal and non-herbal sections use almost entirely different vocabularies.');
    console.log('  Cannot distinguish label words from herbal-dialect function words on this basis.');
  } else {
    console.log('All words appear across sections. Section-specificity is not a distinguishing feature.');
  }
}

main().catch((err) => {
  console.error('[cross-section-test] fatal:', err);
  process.exit(1);
});
