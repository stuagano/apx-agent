// Quire invariance test for new confirmed features: k-init rate and -al suffix rate
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { computeFeature, extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES } from './stat-types.ts';

async function executeSql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method:'POST', headers:{'Content-Type':'application/json',Authorization:`Bearer ${token}`}, body:JSON.stringify({warehouse_id:wid,statement:s,wait_timeout:'50s'}) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns??[]).map((c:any)=>c.name);
  return (d.result?.data_array??[]).map((row:any[])=>Object.fromEntries(cols.map((c:string,i:number)=>[c,row[i]])));
}

function folioToQuire(folioId: string): number {
  const m = folioId.match(/^f(\d+)/i);
  if (!m) return -1;
  const n = parseInt(m[1], 10);
  // Approximate quire mapping for Voynich herbal
  if (n <= 8) return 1;
  if (n <= 16) return 2;
  if (n <= 24) return 3;
  if (n <= 32) return 4;
  if (n <= 40) return 5;
  if (n <= 48) return 6;
  if (n <= 56) return 7;
  if (n <= 64) return 8;
  if (n <= 72) return 9;
  if (n <= 80) return 10;
  if (n <= 88) return 11;
  if (n <= 96) return 12;
  return 13;
}

async function main() {
  const vRows = await executeSql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const eRows = await executeSql("SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section='herbal'");
  
  const familyMap = new Map<string,string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));
  const evaMap = new Map<string,string[]>();
  for (const r of eRows) if (r.folio_id && r.eva_text) { const w=extractEvaWords(r.eva_text); if(w.length>=20) evaMap.set(r.folio_id,w); }

  const TARGET = 'solanaceae';
  const TESTS = [
    { feature: 'k-init rate', direction: 'LOW', label: 'solanaceae LOW k-init (r=-0.441***)' },
    { feature: '-al suffix rate', direction: 'HIGH', label: 'solanaceae HIGH -al (r=+0.382**)' },
    { feature: 'qo-prefix rate', direction: 'HIGH', label: 'solanaceae HIGH qo-prefix (r=+0.532*** — reference)' },
  ];

  for (const { feature, direction, label } of TESTS) {
    console.log(`\n=== ${label} ===`);
    const quireData = new Map<number, { target: number[]; other: number[] }>();
    
    for (const [id, words] of evaMap) {
      const fam = familyMap.get(id) ?? '';
      if (!BOTANICAL_FAMILIES.has(fam)) continue;
      const q = folioToQuire(id);
      if (q < 0) continue;
      if (!quireData.has(q)) quireData.set(q, { target: [], other: [] });
      const val = computeFeature(words, feature) ?? 0;
      if (fam === TARGET) quireData.get(q)!.target.push(val);
      else quireData.get(q)!.other.push(val);
    }

    let consistent = 0, inconsistent = 0, skipped = 0;
    const lines: string[] = [];
    for (const [q, { target, other }] of [...quireData.entries()].sort((a,b)=>a[0]-b[0])) {
      if (target.length === 0 || other.length === 0) { skipped++; continue; }
      const meanT = target.reduce((a,b)=>a+b,0)/target.length;
      const meanO = other.reduce((a,b)=>a+b,0)/other.length;
      const matches = direction === 'HIGH' ? meanT > meanO : meanT < meanO;
      if (matches) consistent++; else inconsistent++;
      const mark = matches ? '✓' : '✗';
      lines.push(`  Quire ${q}: ${TARGET}=${meanT.toFixed(4)} (n=${target.length})  other=${meanO.toFixed(4)} (n=${other.length})  ${mark}`);
    }
    for (const l of lines) console.log(l);
    const total = consistent + inconsistent;
    const pct = total > 0 ? (consistent/total*100).toFixed(0) : 'n/a';
    console.log(`  RESULT: ${consistent}/${total} quires (${pct}%) — ${consistent >= total*0.75 ? 'CONSISTENT' : consistent >= total*0.5 ? 'MIXED' : 'INCONSISTENT'}`);
    if (skipped > 0) console.log(`  (${skipped} quires skipped — no ${TARGET} folios or no other folios)`);
  }
  console.log('\nDone.');
}
main().catch(e=>{ console.error(e); process.exit(1); });
