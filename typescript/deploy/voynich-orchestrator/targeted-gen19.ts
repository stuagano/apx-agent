// Direct tests for remaining genuinely untested dimensions
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { computeFeature, extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES, rankBiserial, shuffle } from './stat-types.ts';

const N_PERMS = 2000;

async function sql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method:'POST', headers:{'Content-Type':'application/json',Authorization:`Bearer ${token}`}, body:JSON.stringify({warehouse_id:wid,statement:s,wait_timeout:'50s'}) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns??[]).map((c:any)=>c.name);
  return (d.result?.data_array??[]).map((row:any[])=>Object.fromEntries(cols.map((c:string,i:number)=>[c,row[i]])));
}

function permTest(groupA: number[], groupB: number[]): { r: number; p: number } {
  const r = rankBiserial(groupA, groupB);
  const all = [...groupA, ...groupB];
  const nA = groupA.length;
  let extreme = 0;
  for (let i = 0; i < N_PERMS; i++) {
    const s = shuffle(all);
    const rPerm = rankBiserial(s.slice(0, nA), s.slice(nA));
    if (Math.abs(rPerm) >= Math.abs(r)) extreme++;
  }
  return { r, p: extreme / N_PERMS };
}

async function main() {
  const vRows = await sql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const eRows = await sql("SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section='herbal'");
  
  const familyMap = new Map<string,string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));
  const evaMap = new Map<string,string[]>();
  for (const r of eRows) if (r.folio_id && r.eva_text) { const w=extractEvaWords(r.eva_text); if(w.length>=20) evaMap.set(r.folio_id,w); }

  // Build per-family feature vectors
  const families: Record<string, number[][]> = {};
  const FEATURES = ['-al suffix rate', 'k-init rate', '-am suffix rate', '-ain suffix rate', '-eedy suffix rate'];
  
  for (const [id, words] of evaMap) {
    const fam = familyMap.get(id) ?? '';
    if (!BOTANICAL_FAMILIES.has(fam)) continue;
    const vals = FEATURES.map(f => computeFeature(words, f) ?? 0);
    if (!families[fam]) families[fam] = FEATURES.map(() => []);
    for (let i = 0; i < FEATURES.length; i++) families[fam][i].push(vals[i]);
  }

  const allBot: number[][] = FEATURES.map(() => []);
  for (const [fam, vecs] of Object.entries(families)) {
    for (let i = 0; i < FEATURES.length; i++) allBot[i].push(...vecs[i]);
  }

  const TESTS: Array<[string, string, string]> = [
    // feature, family_a, family_b
    ['-al suffix rate', 'poppy', 'all-botanical'],
    ['-al suffix rate', 'lily-family', 'all-botanical'],
    ['-al suffix rate', 'artemisia', 'all-botanical'],
    ['k-init rate', 'poppy', 'all-botanical'],
    ['k-init rate', 'lily-family', 'all-botanical'],
    ['k-init rate', 'artemisia', 'all-botanical'],
    ['-am suffix rate', 'plantago', 'all-botanical'],
    ['-am suffix rate', 'poppy', 'all-botanical'],
    ['-ain suffix rate', 'solanaceae', 'all-botanical'],
    ['-ain suffix rate', 'plantago', 'all-botanical'],
    ['-ain suffix rate', 'solanaceae', 'plantago'],
    ['-eedy suffix rate', 'solanaceae', 'plantago'],
  ];

  console.log('=== Targeted gen-19 tests ===\n');
  for (const [feat, fa, fb] of TESTS) {
    const fi = FEATURES.indexOf(feat);
    if (fi < 0) { console.log(`SKIP: ${feat} not in FEATURES`); continue; }
    
    let groupA: number[], groupB: number[];
    if (fa === 'all-botanical') {
      groupA = allBot[fi];
      groupB = families[fb]?.[fi] ?? [];
      [groupA, groupB] = [groupB, groupA]; // flip: family_a is target
    } else if (fb === 'all-botanical') {
      groupA = families[fa]?.[fi] ?? [];
      groupB = allBot[fi];
    } else {
      groupA = families[fa]?.[fi] ?? [];
      groupB = families[fb]?.[fi] ?? [];
    }
    
    if (groupA.length < 3 || groupB.length < 3) {
      console.log(`SKIP ${feat}: ${fa}(n=${groupA.length}) vs ${fb}(n=${groupB.length}) — too few`);
      continue;
    }

    const { r, p } = permTest(groupA, groupB);
    const sig = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : p < 0.1 ? '~' : 'ns';
    console.log(`${feat}: ${fa}(n=${groupA.length}) vs ${fb}(n=${groupB.length})  r=${r.toFixed(3)}  p=${p.toFixed(3)} ${sig}`);
  }
  console.log('\nDone.');
}
main().catch(e=>{ console.error(e); process.exit(1); });
