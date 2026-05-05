// Section vocabulary overlap analysis.
// Tests whether the balneological section shares vocabulary with solanaceae
// despite matching its morphological fingerprint.
//
// If vocabulary overlaps → shared content (what connects bathing and nightshade?)
// If vocabulary is distinct → shared grammar only (topic-independent morphological register)
//
// Also identifies function word candidates (high-frequency across all sections)
// vs. content words (confined to one section).
//
// Run: npx tsx section-vocabulary.ts

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { classifyPlantFamily } from './stat-types.ts';

const ZL_URL = 'https://www.voynich.nu/data/ZL3b-n.txt';
const MIN_WORDS = 20;

const SECTION_NAMES: Record<string, string> = {
  H: 'herbal', B: 'balneo', Z: 'zodiac', P: 'pharma',
  A: 'astro', S: 'stars', C: 'cosmo', T: 'text',
};

async function sql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ warehouse_id: wid, statement: s, wait_timeout: '50s' }) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns ?? []).map((c: any) => c.name);
  return (d.result?.data_array ?? []).map((row: any[]) => Object.fromEntries(cols.map((c: string, i: number) => [c, row[i]])));
}

function extractWords(line: string): string[] {
  let text = line.replace(/<[^>]+>/g, ' ');
  text = text.replace(/\{[^}]*\}/g, '').replace(/\[([^:\]]+):[^\]]*\]/g, '$1').replace(/@\d+;?/g, '').replace(/[!?%$]/g, '');
  return text.split(/[\s.,;-]+/).map(w => w.trim().toLowerCase()).filter(w => w.length >= 2 && /^[a-z]+$/.test(w));
}

interface Folio { id: string; section: string; words: string[] }

function parseZL(text: string): Folio[] {
  const lines = text.split('\n');
  const folios: Folio[] = [];
  let cur: { id: string; section: string; words: string[] } | null = null;
  const flush = () => { if (cur && cur.words.length >= MIN_WORDS) folios.push(cur); };
  for (const line of lines) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const m = t.match(/^<(f\d+[rv]\d*)>/);
    if (m) {
      flush();
      const sm = t.match(/\$I=([A-Za-z])/);
      cur = { id: m[1], section: sm ? sm[1].toUpperCase() : '?', words: [] };
      continue;
    }
    if (cur && /^<f\d+[rv]/.test(t) && t.includes('>')) cur.words.push(...extractWords(t));
  }
  flush();
  return folios;
}

// Jaccard over vocabulary sets
function jaccard(a: Set<string>, b: Set<string>): number {
  let inter = 0;
  for (const w of a) if (b.has(w)) inter++;
  return inter / (a.size + b.size - inter);
}

// Overlap coefficient: |A∩B| / min(|A|, |B|)
function overlap(a: Set<string>, b: Set<string>): number {
  let inter = 0;
  for (const w of a) if (b.has(w)) inter++;
  return inter / Math.min(a.size, b.size);
}

// Build frequency map across a group of folios (total occurrences)
function wordFreq(folios: Folio[]): Map<string, number> {
  const freq = new Map<string, number>();
  for (const f of folios) for (const w of f.words) freq.set(w, (freq.get(w) ?? 0) + 1);
  return freq;
}

// Build folio-presence map: how many folios in group contain each word
function folioPresence(folios: Folio[]): Map<string, number> {
  const pres = new Map<string, number>();
  for (const f of folios) for (const w of new Set(f.words)) pres.set(w, (pres.get(w) ?? 0) + 1);
  return pres;
}

// Top N words by frequency
function topN(freq: Map<string, number>, n: number): Array<{ word: string; count: number }> {
  return [...freq.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([word, count]) => ({ word, count }));
}

async function main() {
  // ── Load data ─────────────────────────────────────────────────────────────
  console.log('Loading ZL transcription...');
  const raw = await fetch(ZL_URL).then(r => r.text());
  const folios = parseZL(raw);
  console.log(`Parsed ${folios.length} folios\n`);

  console.log('Loading solanaceae family assignments from Databricks...');
  const vRows = await sql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const familyMap = new Map<string, string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));
  console.log(`Family assignments: ${familyMap.size} folios\n`);

  // Group folios
  const bySection = new Map<string, Folio[]>();
  for (const f of folios) {
    const arr = bySection.get(f.section) ?? [];
    arr.push(f);
    bySection.set(f.section, arr);
  }

  // Solanaceae = herbal folios whose ID appears in familyMap with 'solanaceae'
  const solanaceae = (bySection.get('H') ?? []).filter(f => familyMap.get(f.id) === 'solanaceae');
  const thistle    = (bySection.get('H') ?? []).filter(f => familyMap.get(f.id) === 'thistle');
  const plantago   = (bySection.get('H') ?? []).filter(f => familyMap.get(f.id) === 'plantago');
  const herbalAll  = bySection.get('H') ?? [];
  const balneo     = bySection.get('B') ?? [];
  const zodiac     = bySection.get('Z') ?? [];
  const stars      = bySection.get('S') ?? [];
  const pharma     = bySection.get('P') ?? [];

  console.log(`Group sizes: solanaceae=${solanaceae.length}  thistle=${thistle.length}  plantago=${plantago.length}  herbal-all=${herbalAll.length}  balneo=${balneo.length}  zodiac=${zodiac.length}  stars=${stars.length}  pharma=${pharma.length}\n`);

  // ── Part 1: Pairwise vocabulary Jaccard ───────────────────────────────────
  console.log('═══ Part 1: Pairwise vocabulary Jaccard ═══\n');
  console.log('(Pooled vocabulary set per group — fraction of shared word types)\n');

  const groups: Array<{ label: string; folios: Folio[] }> = [
    { label: 'solanaceae', folios: solanaceae },
    { label: 'thistle',    folios: thistle },
    { label: 'plantago',   folios: plantago },
    { label: 'balneo',     folios: balneo },
    { label: 'zodiac',     folios: zodiac },
    { label: 'stars',      folios: stars },
    { label: 'pharma',     folios: pharma },
    { label: 'herbal-all', folios: herbalAll },
  ].filter(g => g.folios.length >= 5);

  const vocabs = groups.map(g => ({ label: g.label, vocab: new Set(g.folios.flatMap(f => f.words)) }));

  // Header
  const PAD = 12;
  console.log(' '.padEnd(PAD) + vocabs.map(v => v.label.slice(0, PAD - 1).padEnd(PAD)).join(''));
  console.log('─'.repeat(PAD * (vocabs.length + 1)));
  for (const va of vocabs) {
    const row = vocabs.map(vb => va === vb ? '—'.padEnd(PAD) : jaccard(va.vocab, vb.vocab).toFixed(4).padEnd(PAD));
    console.log(va.label.padEnd(PAD) + row.join(''));
  }

  // ── Part 2: Overlap coefficient (balneo vs. each family) ─────────────────
  console.log('\n═══ Part 2: Overlap coefficient — balneo vs. herbal families ═══\n');
  console.log('(|A∩B| / min(|A|,|B|) — how much of the smaller vocab is shared)\n');
  const balneoVocab = new Set(balneo.flatMap(f => f.words));
  const herbalAllVocab = new Set(herbalAll.flatMap(f => f.words));
  for (const { label, folios } of groups.filter(g => ['solanaceae','thistle','plantago','herbal-all'].includes(g.label))) {
    const v = new Set(folios.flatMap(f => f.words));
    const oc = overlap(balneoVocab, v);
    const jc = jaccard(balneoVocab, v);
    console.log(`  balneo vs ${label.padEnd(14)}  overlap=${oc.toFixed(4)}  jaccard=${jc.toFixed(4)}  |balneo∩${label}|=${[...balneoVocab].filter(w => v.has(w)).length}  |balneo|=${balneoVocab.size}  |${label}|=${v.size}`);
  }

  // ── Part 3: Words exclusive to balneological ─────────────────────────────
  console.log('\n═══ Part 3: Balneological-exclusive words ═══\n');
  console.log('(appear in ≥40% of balneo folios, rate in herbal-all is ≤10% that rate)\n');

  const balneoPresence = folioPresence(balneo);
  const herbalPresence = folioPresence(herbalAll);
  const balneoFreq     = wordFreq(balneo);
  const totalBalneoWords = balneo.reduce((a, f) => a + f.words.length, 0);
  const totalHerbalWords = herbalAll.reduce((a, f) => a + f.words.length, 0);

  type Exclusive = { word: string; bPres: number; hPres: number; bRate: number; hRate: number; lift: number };
  const exclusives: Exclusive[] = [];
  for (const [word, bPres] of balneoPresence) {
    const bPresRate = bPres / balneo.length;
    if (bPresRate < 0.40) continue;
    const hPres = herbalPresence.get(word) ?? 0;
    const hPresRate = hPres / herbalAll.length;
    const bRate = (balneoFreq.get(word) ?? 0) / totalBalneoWords;
    const hRate = (herbalPresence.get(word) ?? 0) / herbalAll.length;  // folio presence rate
    const lift = hPresRate > 0 ? bPresRate / hPresRate : Infinity;
    if (lift >= 2.0) exclusives.push({ word, bPres, hPres, bRate, hRate: hPresRate, lift });
  }
  exclusives.sort((a, b) => b.lift - a.lift);

  console.log(`Balneological-enriched words (≥40% balneo folios, ≥2× lift over herbal):`);
  console.log('Word'.padEnd(16) + 'bPres'.padEnd(8) + 'hPres'.padEnd(8) + 'lift'.padEnd(8) + 'morphology');
  console.log('─'.repeat(60));
  for (const e of exclusives.slice(0, 25)) {
    const morph = [];
    if (e.word.startsWith('qo')) morph.push('qo-prefix');
    if (e.word.endsWith('dy'))   morph.push('-dy');
    if (e.word.endsWith('chy'))  morph.push('-chy');
    if (e.word.startsWith('ch')) morph.push('ch-init');
    if (e.word.length <= 3)      morph.push('short');
    const liftStr = isFinite(e.lift) ? `${e.lift.toFixed(1)}×` : 'balneo-only';
    console.log(e.word.padEnd(16) + `${e.bPres}/${balneo.length}`.padEnd(8) + `${e.hPres}/${herbalAll.length}`.padEnd(8) + liftStr.padEnd(8) + morph.join(', '));
  }

  // ── Part 4: Words shared between balneo and solanaceae but not other families
  console.log('\n═══ Part 4: Balneo ∩ solanaceae — words in both but scarce in other families ═══\n');

  const solPresence = folioPresence(solanaceae);
  const thistlePresence = folioPresence(thistle);
  const plantagoPresence = folioPresence(plantago);

  type SharedEntry = { word: string; bPres: number; sPres: number; tPres: number; pPres: number };
  const shared: SharedEntry[] = [];
  for (const [word, bPres] of balneoPresence) {
    if (bPres / balneo.length < 0.30) continue;
    const sPres = solPresence.get(word) ?? 0;
    if (sPres / solanaceae.length < 0.30) continue;
    const tPres = thistlePresence.get(word) ?? 0;
    const pPres = plantagoPresence.get(word) ?? 0;
    shared.push({ word, bPres, sPres, tPres, pPres });
  }
  shared.sort((a, b) => (b.bPres / balneo.length + b.sPres / solanaceae.length) - (a.bPres / balneo.length + a.sPres / solanaceae.length));

  if (shared.length === 0) {
    console.log('No words appear in ≥30% of both balneo and solanaceae folios.');
  } else {
    console.log(`${shared.length} words appear in ≥30% of both balneo and solanaceae folios:\n`);
    console.log('Word'.padEnd(16) + `B(${balneo.length})`.padEnd(8) + `Sol(${solanaceae.length})`.padEnd(10) + `Thi(${thistle.length})`.padEnd(8) + `Pla(${plantago.length})`);
    console.log('─'.repeat(50));
    for (const e of shared.slice(0, 20)) {
      console.log(e.word.padEnd(16) + `${e.bPres}`.padEnd(8) + `${e.sPres}`.padEnd(10) + `${e.tPres}`.padEnd(8) + `${e.pPres}`);
    }
  }

  // ── Part 5: Function word candidates (cross-section) ─────────────────────
  console.log('\n═══ Part 5: Function word candidates (high frequency across all sections) ═══\n');
  console.log('(words appearing in ≥50% of folios in EVERY eligible section)\n');

  const eligibleSections = ['H', 'B', 'S', 'P'].filter(s => (bySection.get(s)?.length ?? 0) >= 8);
  const sectionPresence = eligibleSections.map(s => ({ s, pres: folioPresence(bySection.get(s)!) }));

  // Candidate function words: present in ≥50% of folios in every section
  const allWords = new Set(folios.flatMap(f => f.words));
  const functionCandidates: Array<{ word: string; rates: number[] }> = [];
  for (const word of allWords) {
    const rates = sectionPresence.map(({ s, pres }) => (pres.get(word) ?? 0) / (bySection.get(s)?.length ?? 1));
    if (rates.every(r => r >= 0.50)) {
      functionCandidates.push({ word, rates });
    }
  }
  functionCandidates.sort((a, b) => b.rates.reduce((x, y) => x + y) - a.rates.reduce((x, y) => x + y));

  console.log(`${functionCandidates.length} words appear in ≥50% of folios in all of: ${eligibleSections.join(', ')}\n`);
  console.log('Word'.padEnd(16) + eligibleSections.map(s => `${s}(${(SECTION_NAMES[s]??s).slice(0,5)})`.padEnd(10)).join('') + 'morphology');
  console.log('─'.repeat(16 + eligibleSections.length * 10 + 20));
  for (const fc of functionCandidates.slice(0, 30)) {
    const morph = [];
    if (fc.word.startsWith('qo')) morph.push('qo');
    if (fc.word.endsWith('dy'))   morph.push('-dy');
    if (fc.word.endsWith('chy'))  morph.push('-chy');
    if (fc.word.startsWith('ch')) morph.push('ch-init');
    if (fc.word.length <= 3)      morph.push('short');
    console.log(fc.word.padEnd(16) + fc.rates.map(r => r.toFixed(2).padEnd(10)).join('') + morph.join(', '));
  }

  // ── Part 6: Morphological class of balneo-exclusive words ────────────────
  console.log('\n═══ Part 6: Morphological breakdown — balneo-exclusive vs. solanaceae-exclusive ═══\n');

  function morphBreakdown(words: string[], label: string) {
    const n = words.length;
    const qo  = words.filter(w => w.startsWith('qo')).length;
    const dy  = words.filter(w => w.endsWith('dy')).length;
    const chy = words.filter(w => w.endsWith('chy')).length;
    const ch  = words.filter(w => w.startsWith('ch')).length;
    const sh  = words.filter(w => w.length <= 3).length;
    console.log(`${label.padEnd(20)}  n=${n}  qo=${(qo/n).toFixed(2)}  -dy=${(dy/n).toFixed(2)}  -chy=${(chy/n).toFixed(2)}  ch-init=${(ch/n).toFixed(2)}  short=${(sh/n).toFixed(2)}`);
  }

  // Words unique to each section's top-40%-present words
  const balneoExclWords = exclusives.map(e => e.word);
  const solExclEntries: Exclusive[] = [];
  for (const [word, sPres] of solPresence) {
    const sPresRate = sPres / solanaceae.length;
    if (sPresRate < 0.40) continue;
    const bPres = balneoPresence.get(word) ?? 0;
    const bPresRate = bPres / balneo.length;
    const lift = bPresRate > 0 ? sPresRate / bPresRate : Infinity;
    if (lift >= 2.0) solExclEntries.push({ word, bPres, hPres: 0, bRate: 0, hRate: bPresRate, lift });
  }
  const solExclWords = solExclEntries.map(e => e.word);

  console.log('Morphological profile of section-exclusive vocabulary (words common in one section, rare in the other):\n');
  morphBreakdown(balneoExclWords, 'balneo-exclusive');
  morphBreakdown(solExclWords, 'solanaceae-exclusive');

  // All balneo words vs all solanaceae words
  const allBalneoWords = balneo.flatMap(f => [...new Set(f.words)]);
  const allSolWords = solanaceae.flatMap(f => [...new Set(f.words)]);
  console.log('');
  morphBreakdown(allBalneoWords, 'balneo (all words)');
  morphBreakdown(allSolWords, 'solanaceae (all words)');

  console.log('\nDone.');
}

main().catch(e => { console.error(e); process.exit(1); });
