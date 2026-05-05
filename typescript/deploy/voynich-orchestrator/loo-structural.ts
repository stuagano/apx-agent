// LOO classifier — test whether structural features improve accuracy over original 6
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { computeFeature, extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES } from './stat-types.ts';

const LOO_FAMILIES = ['solanaceae', 'thistle', 'plantago', 'poppy'];
const MIN_N = 5;

async function sql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ warehouse_id: wid, statement: s, wait_timeout: '50s' }) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns ?? []).map((c: any) => c.name);
  return (d.result?.data_array ?? []).map((row: any[]) => Object.fromEntries(cols.map((c: string, i: number) => [c, row[i]])));
}

function zNorm(vals: number[]): { mean: number; std: number } {
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  const std = Math.sqrt(vals.reduce((a, b) => a + (b - mean) ** 2, 0) / vals.length) || 1;
  return { mean, std };
}

function euclidean(a: number[], b: number[]): number {
  return Math.sqrt(a.reduce((s, v, i) => s + (v - b[i]) ** 2, 0));
}

function looClassify(data: Array<{ family: string; features: number[] }>): { accuracy: number; perFamily: Record<string, { correct: number; total: number }> } {
  const nFeats = data[0].features.length;
  // Z-score normalization params from full data
  const norms = Array.from({ length: nFeats }, (_, fi) => zNorm(data.map(d => d.features[fi])));

  let correct = 0;
  const perFamily: Record<string, { correct: number; total: number }> = {};
  for (const fam of LOO_FAMILIES) perFamily[fam] = { correct: 0, total: 0 };

  for (let i = 0; i < data.length; i++) {
    const held = data[i];
    const rest = data.filter((_, j) => j !== i);

    // Compute centroids from rest
    const centroids: Record<string, number[]> = {};
    for (const fam of LOO_FAMILIES) {
      const members = rest.filter(d => d.family === fam);
      if (members.length === 0) continue;
      centroids[fam] = Array.from({ length: nFeats }, (_, fi) => {
        const mean = norms[fi].mean, std = norms[fi].std;
        return members.reduce((s, d) => s + (d.features[fi] - mean) / std, 0) / members.length;
      });
    }

    // Classify held-out
    const heldNorm = held.features.map((v, fi) => (v - norms[fi].mean) / norms[fi].std);
    let bestFam = '', bestDist = Infinity;
    for (const [fam, centroid] of Object.entries(centroids)) {
      const dist = euclidean(heldNorm, centroid);
      if (dist < bestDist) { bestDist = dist; bestFam = fam; }
    }

    if (bestFam === held.family) correct++;
    perFamily[held.family].total++;
    if (bestFam === held.family) perFamily[held.family].correct++;
  }

  return { accuracy: correct / data.length, perFamily };
}

async function main() {
  const vRows = await sql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const eRows = await sql("SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section='herbal'");

  const familyMap = new Map<string, string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));
  const evaMap = new Map<string, string[]>();
  for (const r of eRows) if (r.folio_id && r.eva_text) { const w = extractEvaWords(r.eva_text); if (w.length >= 20) evaMap.set(r.folio_id, w); }

  // Filter to LOO families with MIN_N
  const familyCounts: Record<string, number> = {};
  for (const fam of [...familyMap.values()]) familyCounts[fam] = (familyCounts[fam] ?? 0) + 1;
  const eligible = LOO_FAMILIES.filter(f => (familyCounts[f] ?? 0) >= MIN_N);

  const FEATURE_SETS: Record<string, string[]> = {
    'baseline (6)':          ['qo-prefix rate', '-chy suffix rate', '-dy suffix rate', 'ch-init rate', 'short word rate', 'unique word ratio'],
    '6 + l-containing':      ['qo-prefix rate', '-chy suffix rate', '-dy suffix rate', 'ch-init rate', 'short word rate', 'unique word ratio', 'l-containing rate'],
    '6 + word-length CV':    ['qo-prefix rate', '-chy suffix rate', '-dy suffix rate', 'ch-init rate', 'short word rate', 'unique word ratio', 'word-length CV'],
    '6 + both structural':   ['qo-prefix rate', '-chy suffix rate', '-dy suffix rate', 'ch-init rate', 'short word rate', 'unique word ratio', 'l-containing rate', 'word-length CV'],
    '6 + gallows':           ['qo-prefix rate', '-chy suffix rate', '-dy suffix rate', 'ch-init rate', 'short word rate', 'unique word ratio', 'gallows rate'],
    '6 + all 3 structural':  ['qo-prefix rate', '-chy suffix rate', '-dy suffix rate', 'ch-init rate', 'short word rate', 'unique word ratio', 'l-containing rate', 'word-length CV', 'gallows rate'],
  };

  console.log('=== LOO nearest-centroid classifier — structural features ===\n');
  console.log(`Families: ${eligible.join(', ')}\n`);

  for (const [label, feats] of Object.entries(FEATURE_SETS)) {
    const data: Array<{ family: string; features: number[] }> = [];
    for (const [id, words] of evaMap) {
      const fam = familyMap.get(id) ?? '';
      if (!eligible.includes(fam)) continue;
      data.push({ family: fam, features: feats.map(f => computeFeature(words, f) ?? 0) });
    }

    const { accuracy, perFamily } = looClassify(data);
    const pct = (accuracy * 100).toFixed(1);
    const n = data.length;
    const perFamStr = eligible.map(f => `${f.slice(0, 3)}:${((perFamily[f]?.correct ?? 0) / (perFamily[f]?.total ?? 1) * 100).toFixed(0)}%`).join('  ');
    console.log(`${label.padEnd(22)} ${pct}% (${Math.round(accuracy * n)}/${n})  [${perFamStr}]`);
  }

  console.log('\nChance baseline: 25.0%  |  Majority class: 41.3%\nDone.');
}
main().catch(e => { console.error(e); process.exit(1); });
