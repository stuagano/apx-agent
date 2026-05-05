// Gen-20 structural feature scan: gallows rate, l-containing rate, consecutive-repeat rate, word-length CV
// All 4 features × 7 families vs all-botanical + all 21 pairwise combinations
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { computeFeature, extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES, rankBiserial, shuffle } from './stat-types.ts';

const N_PERMS = 2000;
const FEATURES = ['gallows rate', 'l-containing rate', 'consecutive-repeat rate', 'word-length CV'];
const FAMILIES_ORDERED = ['solanaceae', 'thistle', 'plantago', 'poppy', 'lily-family', 'artemisia', 'mint-family'];

async function sql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ warehouse_id: wid, statement: s, wait_timeout: '50s' }) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns ?? []).map((c: any) => c.name);
  return (d.result?.data_array ?? []).map((row: any[]) => Object.fromEntries(cols.map((c: string, i: number) => [c, row[i]])));
}

function permTest(groupA: number[], groupB: number[]): { r: number; p: number } {
  const r = rankBiserial(groupA, groupB);
  const all = [...groupA, ...groupB];
  const nA = groupA.length;
  let extreme = 0;
  for (let i = 0; i < N_PERMS; i++) {
    const s = shuffle(all);
    if (Math.abs(rankBiserial(s.slice(0, nA), s.slice(nA))) >= Math.abs(r)) extreme++;
  }
  return { r, p: extreme / N_PERMS };
}

async function main() {
  const vRows = await sql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const eRows = await sql("SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section='herbal'");

  const familyMap = new Map<string, string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));
  const evaMap = new Map<string, string[]>();
  for (const r of eRows) if (r.folio_id && r.eva_text) { const w = extractEvaWords(r.eva_text); if (w.length >= 20) evaMap.set(r.folio_id, w); }

  // Per-family feature vectors: for consecutive-repeat rate we need word sequence, not bag
  const familyVecs: Record<string, Record<string, number[]>> = {};
  for (const fam of FAMILIES_ORDERED) familyVecs[fam] = Object.fromEntries(FEATURES.map(f => [f, [] as number[]]));
  const allBotVecs: Record<string, number[]> = Object.fromEntries(FEATURES.map(f => [f, [] as number[]]));

  for (const [id, words] of evaMap) {
    const fam = familyMap.get(id) ?? '';
    if (!BOTANICAL_FAMILIES.has(fam)) continue;
    for (const feat of FEATURES) {
      const val = computeFeature(words, feat) ?? 0;
      allBotVecs[feat].push(val);
      if (familyVecs[fam]) familyVecs[fam][feat].push(val);
    }
  }

  const results: Array<{ feat: string; fa: string; fb: string; r: number; p: number; nA: number; nB: number }> = [];

  // vs all-botanical
  for (const feat of FEATURES) {
    for (const fam of FAMILIES_ORDERED) {
      const groupA = familyVecs[fam]?.[feat] ?? [];
      if (groupA.length < 3) continue;
      const groupB = allBotVecs[feat];
      const { r, p } = permTest(groupA, groupB);
      results.push({ feat, fa: fam, fb: 'all-botanical', r, p, nA: groupA.length, nB: groupB.length });
    }
  }

  // pairwise
  for (const feat of FEATURES) {
    for (let i = 0; i < FAMILIES_ORDERED.length; i++) {
      for (let j = i + 1; j < FAMILIES_ORDERED.length; j++) {
        const fa = FAMILIES_ORDERED[i], fb = FAMILIES_ORDERED[j];
        const gA = familyVecs[fa]?.[feat] ?? [], gB = familyVecs[fb]?.[feat] ?? [];
        if (gA.length < 3 || gB.length < 3) continue;
        const { r, p } = permTest(gA, gB);
        results.push({ feat, fa, fb, r, p, nA: gA.length, nB: gB.length });
      }
    }
  }

  // Print significant results
  console.log(`\n=== Structural feature scan (N_PERMS=${N_PERMS}) ===\n`);
  console.log('SIGNIFICANT (p<0.05):');
  const sig = results.filter(r => r.p < 0.05).sort((a, b) => a.p - b.p);
  for (const r of sig) {
    const mark = r.p < 0.001 ? '***' : r.p < 0.01 ? '**' : '*';
    console.log(`  ${r.feat}: ${r.fa}(n=${r.nA}) vs ${r.fb}(n=${r.nB})  r=${r.r.toFixed(3)}  p=${r.p.toFixed(3)} ${mark}`);
  }

  console.log('\nBORDERLINE (p<0.10):');
  const border = results.filter(r => r.p >= 0.05 && r.p < 0.10).sort((a, b) => a.p - b.p);
  for (const r of border) {
    console.log(`  ${r.feat}: ${r.fa}(n=${r.nA}) vs ${r.fb}(n=${r.nB})  r=${r.r.toFixed(3)}  p=${r.p.toFixed(3)} ~`);
  }

  console.log(`\nTotal tests: ${results.length}, significant: ${sig.length} (${(sig.length/results.length*100).toFixed(1)}%)`);
  console.log('Done.');
}
main().catch(e => { console.error(e); process.exit(1); });
