/**
 * Section-specificity test for label words.
 *
 * The label-word-test.ts found 13 statistically significant label words
 * (once_rate ≥ 0.70, family-concentrated, p<0.05):
 *   solanaceae: olaiin, qokeody, qockhey, ckhey, sy, oldaiin, cphey, okor, qockhol, qokan
 *   thistle: kchy, ydaiin, ty
 *
 * If these are plant-name identifiers or herbal-specific vocabulary, they
 * should appear at low rates in non-herbal sections (zodiac, balneological,
 * pharmaceutical, astronomical, etc.).
 *
 * If they are grammatical/corpus-wide words (high-frequency function words
 * that just happen to be once-per-folio due to low base rate), they should
 * appear at comparable rates across all sections.
 *
 * Contrast: known running-text words (daiin, aiin, chol, shedy, qotaiin)
 * should be section-diffuse to validate the method.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx section-specificity-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const LABEL_WORDS = [
  // solanaceae labels (p<0.05)
  'olaiin', 'qokeody', 'qockhey', 'ckhey', 'sy', 'oldaiin', 'cphey', 'okor', 'qockhol', 'qokan',
  // thistle labels (p<0.05)
  'kchy', 'ydaiin', 'ty',
];

const RUNNING_TEXT_WORDS = [
  // high-frequency running-text words from solanaceae analysis
  'daiin', 'aiin', 'chol', 'shedy', 'qotaiin', 'lchedy', 'qokeedy',
];

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '50s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?: { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?: { state?: string; error?: { message?: string } };
  };
  if (data.status?.state === 'FAILED') throw new Error(`SQL failed: ${data.status.error?.message}`);
  const cols = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  return (data.result?.data_array ?? []).map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

function extractWords(evaText: string): Set<string> {
  const words = evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
  return new Set(words);
}

async function main(): Promise<void> {
  console.log('section-specificity-test — are label words herbal-specific or corpus-wide?');
  console.log('');

  const evaRows = await executeSql(`
    SELECT folio_id, section, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    ORDER BY section, folio_id
  `);

  // Build section → folio → word-set
  interface FolioEntry { folioId: string; section: string; words: Set<string>; }
  const folios: FolioEntry[] = [];
  for (const r of evaRows) {
    if (!r.folio_id || !r.section || !r.eva_text) continue;
    const words = extractWords(r.eva_text);
    if (words.size < 5) continue;
    folios.push({ folioId: r.folio_id, section: r.section, words });
  }

  // Section sizes
  const sectionFolios = new Map<string, FolioEntry[]>();
  for (const f of folios) {
    const s = sectionFolios.get(f.section) ?? [];
    s.push(f);
    sectionFolios.set(f.section, s);
  }

  const sections = [...sectionFolios.keys()].sort();
  console.log('Sections found:');
  for (const s of sections) {
    console.log(`  ${s.padEnd(20)} ${sectionFolios.get(s)!.length} folios`);
  }
  console.log(`  ${'TOTAL'.padEnd(20)} ${folios.length} folios`);
  console.log('');

  // Per-word, per-section: n folios with word / n folios in section
  function sectionRate(word: string, sectionName: string): number {
    const sf = sectionFolios.get(sectionName) ?? [];
    if (sf.length === 0) return 0;
    const withWord = sf.filter((f) => f.words.has(word)).length;
    return withWord / sf.length;
  }

  // Herbal specificity ratio: herbal_rate / mean(non-herbal_rate)
  function specificityRatio(word: string): number {
    const herbalRate = sectionRate(word, 'herbal');
    const nonHerbalSections = sections.filter((s) => s !== 'herbal');
    const nonHerbalRates = nonHerbalSections.map((s) => sectionRate(word, s)).filter((r) => r > 0);
    if (nonHerbalRates.length === 0) return herbalRate > 0 ? Infinity : 1;
    const meanNonHerbal = nonHerbalRates.reduce((a, b) => a + b, 0) / nonHerbalRates.length;
    return meanNonHerbal === 0 ? Infinity : herbalRate / meanNonHerbal;
  }

  const ALL_WORDS = [...LABEL_WORDS, ...RUNNING_TEXT_WORDS];

  // Full rate table
  console.log('=== OCCURRENCE RATE PER SECTION (fraction of folios with word) ===');
  console.log('');

  // Header
  const secWidth = 8;
  const wordWidth = 14;
  const header = 'Word'.padEnd(wordWidth) + 'Type'.padEnd(8) +
    sections.map((s) => s.slice(0, secWidth).padEnd(secWidth + 2)).join('') + 'Herbal/Other';
  console.log(header);
  console.log('-'.repeat(header.length));

  for (const word of ALL_WORDS) {
    const isLabel = LABEL_WORDS.includes(word);
    const rates = sections.map((s) => sectionRate(word, s));
    const ratio = specificityRatio(word);
    const ratioStr = isFinite(ratio) ? `${ratio.toFixed(1)}×` : 'herbal-only';
    const rateStrs = rates.map((r) => (r === 0 ? '—' : r.toFixed(2)).padEnd(secWidth + 2));
    console.log(
      word.padEnd(wordWidth) +
      (isLabel ? 'label' : 'text').padEnd(8) +
      rateStrs.join('') +
      ratioStr,
    );
  }

  console.log('');
  console.log(`Sections order: ${sections.join(', ')}`);

  // Summary: herbal specificity ratios
  console.log('');
  console.log('=== HERBAL SPECIFICITY (herbal_rate / mean_non-herbal_rate) ===');
  console.log('');
  console.log('Higher = more herbal-specific. "herbal-only" = absent from all non-herbal sections.');
  console.log('');

  const labelRatios   = LABEL_WORDS.map((w) => ({ word: w, ratio: specificityRatio(w), herbalRate: sectionRate(w, 'herbal') }));
  const textRatios    = RUNNING_TEXT_WORDS.map((w) => ({ word: w, ratio: specificityRatio(w), herbalRate: sectionRate(w, 'herbal') }));

  console.log('Label words:');
  console.log('Word'.padEnd(16) + 'Herbal rate'.padEnd(14) + 'Specificity');
  console.log('-'.repeat(44));
  for (const { word, ratio, herbalRate } of labelRatios.sort((a, b) => b.ratio - a.ratio)) {
    const ratioStr = isFinite(ratio) ? `${ratio.toFixed(1)}×` : 'herbal-only';
    console.log(word.padEnd(16) + herbalRate.toFixed(3).padEnd(14) + ratioStr);
  }

  console.log('');
  console.log('Running-text words (contrast):');
  console.log('Word'.padEnd(16) + 'Herbal rate'.padEnd(14) + 'Specificity');
  console.log('-'.repeat(44));
  for (const { word, ratio, herbalRate } of textRatios.sort((a, b) => b.ratio - a.ratio)) {
    const ratioStr = isFinite(ratio) ? `${ratio.toFixed(1)}×` : 'herbal-only';
    console.log(word.padEnd(16) + herbalRate.toFixed(3).padEnd(14) + ratioStr);
  }

  // Count herbal-only label words
  const herbalOnlyLabels = labelRatios.filter((r) => !isFinite(r.ratio) || r.ratio > 5);
  const herbalOnlyText   = textRatios.filter((r) => !isFinite(r.ratio) || r.ratio > 5);

  console.log('');
  console.log(`Label words with specificity >5×: ${herbalOnlyLabels.length}/${LABEL_WORDS.length}`);
  console.log(`Text words with specificity >5×:  ${herbalOnlyText.length}/${RUNNING_TEXT_WORDS.length}`);
  console.log('');

  const medianLabelRatio = [...labelRatios]
    .filter((r) => isFinite(r.ratio))
    .sort((a, b) => a.ratio - b.ratio)
    .map((r) => r.ratio);
  const medianTextRatio  = [...textRatios]
    .filter((r) => isFinite(r.ratio))
    .sort((a, b) => a.ratio - b.ratio)
    .map((r) => r.ratio);

  const median = (arr: number[]) => arr.length ? arr[Math.floor(arr.length / 2)] : NaN;
  console.log(`Median finite specificity — label: ${median(medianLabelRatio).toFixed(1)}×  text: ${median(medianTextRatio).toFixed(1)}×`);
  console.log('');

  // Verdict
  const stronglySpecific = herbalOnlyLabels.length;
  if (stronglySpecific >= Math.ceil(LABEL_WORDS.length * 0.6)) {
    console.log(`VERDICT: ${stronglySpecific}/${LABEL_WORDS.length} label words are herbal-specific (>5× rate vs. other sections).`);
    console.log('  Label words are not corpus-wide grammatical tokens — they are concentrated');
    console.log('  in the herbal section. Consistent with plant-name identifiers or herbal-specific');
    console.log('  vocabulary rather than general EVA function words.');
  } else if (stronglySpecific >= Math.ceil(LABEL_WORDS.length * 0.4)) {
    console.log(`VERDICT: Mixed. ${stronglySpecific}/${LABEL_WORDS.length} label words are highly herbal-specific.`);
    console.log('  Some label words appear across sections, suggesting a mix of herbal-specific');
    console.log('  and corpus-wide function words in the label tier.');
  } else {
    console.log(`VERDICT: Label words appear across sections at comparable rates.`);
    console.log('  These may be corpus-wide grammatical words whose once-per-folio distribution');
    console.log('  reflects low base rate rather than herbal-specific semantics.');
  }
}

main().catch((err) => {
  console.error('[section-specificity-test] fatal:', err);
  process.exit(1);
});
