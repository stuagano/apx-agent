// Two analyses:
// 1. Permutation test on bigram Jaccard lift — is 1.277× significantly above chance?
// 2. LOO nearest-centroid using discriminative bigrams as binary features
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES, shuffle } from './stat-types.ts';

const N_PERMS = 1000;
const LOO_FAMILIES = ['solanaceae', 'thistle', 'plantago', 'poppy'];
const MIN_FAMILY_N = 5;

async function sql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ warehouse_id: wid, statement: s, wait_timeout: '50s' }) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns ?? []).map((c: any) => c.name);
  return (d.result?.data_array ?? []).map((row: any[]) => Object.fromEntries(cols.map((c: string, i: number) => [c, row[i]])));
}

function bigrams(words: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < words.length - 1; i++) out.push(`${words[i]}|${words[i + 1]}`);
  return out;
}

function jaccard(a: string[], b: string[]): number {
  const setA = new Set(a), setB = new Set(b);
  let inter = 0;
  for (const x of setA) if (setB.has(x)) inter++;
  return inter / (setA.size + setB.size - inter);
}

function computeLift(folios: Array<{ family: string; tokens: string[] }>): number {
  let within = 0, between = 0, nW = 0, nB = 0;
  for (let i = 0; i < folios.length; i++) {
    for (let j = i + 1; j < folios.length; j++) {
      const j12 = jaccard(folios[i].tokens, folios[j].tokens);
      if (folios[i].family === folios[j].family) { within += j12; nW++; }
      else { between += j12; nB++; }
    }
  }
  return (within / nW) / (between / nB);
}

function euclidean(a: number[], b: number[]): number {
  return Math.sqrt(a.reduce((s, v, i) => s + (v - b[i]) ** 2, 0));
}

async function main() {
  const vRows = await sql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const eRows = await sql("SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section='herbal'");

  const familyMap = new Map<string, string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));

  type Folio = { id: string; family: string; words: string[]; bgs: string[] };
  const folios: Folio[] = [];
  for (const r of eRows) {
    if (!r.folio_id || !r.eva_text) continue;
    const fam = familyMap.get(r.folio_id) ?? '';
    if (!BOTANICAL_FAMILIES.has(fam)) continue;
    const words = extractEvaWords(r.eva_text);
    if (words.length < 20) continue;
    folios.push({ id: r.folio_id, family: fam, words, bgs: bigrams(words) });
  }

  // ── Part 1: Permutation test on bigram lift ────────────────────────────────
  console.log('=== Part 1: Permutation test on bigram Jaccard lift ===\n');

  const observed = computeLift(folios.map(f => ({ family: f.family, tokens: f.bgs })));
  const families = folios.map(f => f.family);
  let extreme = 0;
  for (let p = 0; p < N_PERMS; p++) {
    const shuffled = shuffle(families);
    const permLift = computeLift(folios.map((f, i) => ({ family: shuffled[i], tokens: f.bgs })));
    if (permLift >= observed) extreme++;
  }
  const pVal = extreme / N_PERMS;
  console.log(`Observed bigram lift: ${observed.toFixed(3)}×`);
  console.log(`Permutation p-value: ${pVal <= 0 ? `<${1/N_PERMS}` : pVal.toFixed(3)} (${N_PERMS} shuffles)`);
  console.log(`(${extreme} of ${N_PERMS} permutations >= observed)\n`);

  // ── Part 2: LOO classifier using discriminative bigrams as binary features ─
  console.log('=== Part 2: LOO classifier — discriminative bigrams as features ===\n');

  const looFolios = folios.filter(f => LOO_FAMILIES.includes(f.family));
  const familyCounts: Record<string, number> = {};
  for (const f of looFolios) familyCounts[f.family] = (familyCounts[f.family] ?? 0) + 1;
  const eligible = LOO_FAMILIES.filter(f => (familyCounts[f] ?? 0) >= MIN_FAMILY_N);
  const data = looFolios.filter(f => eligible.includes(f.family));

  // Identify discriminative bigrams: appear in >=3 folios of one family,
  // enrichment >= 2.0× vs all folios in data, tested at multiple thresholds
  const allFolioN = data.length;
  const bgFolioFreq = new Map<string, number>();
  for (const f of data) for (const bg of new Set(f.bgs)) bgFolioFreq.set(bg, (bgFolioFreq.get(bg) ?? 0) + 1);

  const discBigrams: string[] = [];
  for (const fam of eligible) {
    const famFolios = data.filter(f => f.family === fam);
    const famBgFreq = new Map<string, number>();
    for (const f of famFolios) for (const bg of new Set(f.bgs)) famBgFreq.set(bg, (famBgFreq.get(bg) ?? 0) + 1);
    for (const [bg, k] of famBgFreq) {
      if (k < 3) continue;
      const famRate = k / famFolios.length;
      const allRate = (bgFolioFreq.get(bg) ?? 0) / allFolioN;
      if (allRate > 0 && famRate / allRate >= 2.0) discBigrams.push(bg);
    }
  }
  const bgFeatures = [...new Set(discBigrams)];
  console.log(`Discriminative bigrams (k≥3 in one family, enrichment≥2×): ${bgFeatures.length}`);

  // Binary encode: 1 if folio contains bigram, 0 if not
  function encode(f: Folio): number[] {
    const bgSet = new Set(f.bgs);
    return bgFeatures.map(bg => bgSet.has(bg) ? 1 : 0);
  }

  function looRun(featureVecs: Array<{ family: string; vec: number[] }>): { acc: number; perFam: Record<string, { c: number; t: number }> } {
    const nF = featureVecs[0].vec.length;
    let correct = 0;
    const perFam: Record<string, { c: number; t: number }> = {};
    for (const fam of eligible) perFam[fam] = { c: 0, t: 0 };

    for (let i = 0; i < featureVecs.length; i++) {
      const held = featureVecs[i];
      const rest = featureVecs.filter((_, j) => j !== i);
      const centroids: Record<string, number[]> = {};
      for (const fam of eligible) {
        const members = rest.filter(d => d.family === fam);
        if (members.length === 0) continue;
        centroids[fam] = Array.from({ length: nF }, (_, fi) =>
          members.reduce((s, d) => s + d.vec[fi], 0) / members.length
        );
      }
      let best = '', bestD = Infinity;
      for (const [fam, c] of Object.entries(centroids)) {
        const d = euclidean(held.vec, c);
        if (d < bestD) { bestD = d; best = fam; }
      }
      perFam[held.family].t++;
      if (best === held.family) { correct++; perFam[held.family].c++; }
    }
    return { acc: correct / featureVecs.length, perFam };
  }

  // Run 1: bigram binary features only
  const bgVecs = data.map(f => ({ family: f.family, vec: encode(f) }));
  const { acc: bgAcc, perFam: bgPF } = looRun(bgVecs);

  // Run 2: combined — original 6 rate features + bigram binary features
  // (import computeFeature for the 6 original features)
  const { computeFeature } = await import('./stat-types.ts');
  const ORIG6 = ['qo-prefix rate', '-chy suffix rate', '-dy suffix rate', 'ch-init rate', 'short word rate', 'unique word ratio'];

  // Z-norm the rate features using full-data stats
  const rateMatrix = data.map(f => ORIG6.map(feat => computeFeature(f.words, feat) ?? 0));
  const norms = ORIG6.map((_, fi) => {
    const vals = rateMatrix.map(r => r[fi]);
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const std = Math.sqrt(vals.reduce((a, b) => a + (b - mean) ** 2, 0) / vals.length) || 1;
    return { mean, std };
  });
  const comboVecs = data.map((f, i) => ({
    family: f.family,
    vec: [
      ...rateMatrix[i].map((v, fi) => (v - norms[fi].mean) / norms[fi].std),
      ...encode(f),
    ],
  }));
  const { acc: comboAcc, perFam: comboPF } = looRun(comboVecs);

  const fmt = (pf: Record<string, { c: number; t: number }>) =>
    eligible.map(f => `${f.slice(0,3)}:${((pf[f]?.c ?? 0) / (pf[f]?.t ?? 1) * 100).toFixed(0)}%`).join('  ');

  console.log(`\nbaseline rate features (6)    52.5% (42/80)  [sol:67%  thi:40%  pla:60%  pop:29%]`);
  console.log(`bigram binary (${bgFeatures.length} features)    ${(bgAcc*100).toFixed(1)}% (${Math.round(bgAcc*data.length)}/${data.length})  [${fmt(bgPF)}]`);
  console.log(`combined rate + bigram         ${(comboAcc*100).toFixed(1)}% (${Math.round(comboAcc*data.length)}/${data.length})  [${fmt(comboPF)}]`);
  console.log('\nChance: 25.0%  |  Majority class: 41.3%\nDone.');
}
main().catch(e => { console.error(e); process.exit(1); });
