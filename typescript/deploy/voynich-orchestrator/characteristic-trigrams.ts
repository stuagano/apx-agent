// Find the characteristic trigrams driving the 3.098× family lift.
// For each family: rank trigrams by enrichment vs all-botanical, permutation-test the top candidates.
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES, shuffle } from './stat-types.ts';

const N_PERMS = 2000;

async function sql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ warehouse_id: wid, statement: s, wait_timeout: '50s' }) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns ?? []).map((c: any) => c.name);
  return (d.result?.data_array ?? []).map((row: any[]) => Object.fromEntries(cols.map((c: string, i: number) => [c, row[i]])));
}

function trigrams(words: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i <= words.length - 3; i++) out.push(`${words[i]}|${words[i+1]}|${words[i+2]}`);
  return out;
}

async function main() {
  const vRows = await sql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const eRows = await sql("SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section='herbal'");

  const familyMap = new Map<string, string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));

  type Folio = { id: string; family: string; words: string[]; tgSet: Set<string>; tgList: string[] };
  const folios: Folio[] = [];
  for (const r of eRows) {
    if (!r.folio_id || !r.eva_text) continue;
    const fam = familyMap.get(r.folio_id) ?? '';
    if (!BOTANICAL_FAMILIES.has(fam)) continue;
    const words = extractEvaWords(r.eva_text);
    if (words.length < 20) continue;
    const tgList = trigrams(words);
    folios.push({ id: r.folio_id, family: fam, words, tgSet: new Set(tgList), tgList });
  }

  const allN = folios.length;
  const tgFolioFreq = new Map<string, number>();
  for (const f of folios) for (const tg of f.tgSet) tgFolioFreq.set(tg, (tgFolioFreq.get(tg) ?? 0) + 1);

  const FAMILIES = ['solanaceae', 'thistle', 'plantago'];

  for (const fam of FAMILIES) {
    const famFolios = folios.filter(f => f.family === fam);
    const famN = famFolios.length;

    // Count folio-level frequency for each trigram
    const famTgFreq = new Map<string, number>();
    for (const f of famFolios) for (const tg of f.tgSet) famTgFreq.set(tg, (famTgFreq.get(tg) ?? 0) + 1);

    // Rank by enrichment, minimum k=2
    type Entry = { tg: string; k: number; famRate: number; allRate: number; enrich: number };
    const entries: Entry[] = [];
    for (const [tg, k] of famTgFreq) {
      if (k < 2) continue;
      const famRate = k / famN;
      const allRate = (tgFolioFreq.get(tg) ?? 0) / allN;
      if (allRate === 0) continue;
      entries.push({ tg, k, famRate, allRate, enrich: famRate / allRate });
    }
    entries.sort((a, b) => b.enrich - a.enrich);

    console.log(`\n═══ ${fam.toUpperCase()} (n=${famN}) — top trigrams by enrichment ═══\n`);

    if (entries.length === 0) {
      console.log('  No trigrams appear in ≥2 folios.');
      continue;
    }

    // Show top 12
    const top = entries.slice(0, 12);
    for (const e of top) {
      const [w1, w2, w3] = e.tg.split('|');
      console.log(`  "${w1}  ${w2}  ${w3}"  k=${e.k}/${famN}  famRate=${e.famRate.toFixed(2)}  allRate=${e.allRate.toFixed(3)}  enrich=${e.enrich.toFixed(1)}×`);
    }

    // Permutation test on top-5 candidates
    console.log(`\n  Permutation tests (top 5, N=${N_PERMS}):`);
    const families = folios.map(f => f.family);
    for (const e of top.slice(0, 5)) {
      // Observed: fraction of family folios containing this trigram
      const observed = e.famRate;
      let extreme = 0;
      for (let p = 0; p < N_PERMS; p++) {
        const sh = shuffle(families);
        const permFamFolios = folios.filter((_, i) => sh[i] === fam);
        const permRate = permFamFolios.filter(f => f.tgSet.has(e.tg)).length / famN;
        if (permRate >= observed) extreme++;
      }
      const pv = extreme / N_PERMS;
      const sig = pv < 0.001 ? '***' : pv < 0.01 ? '**' : pv < 0.05 ? '*' : pv < 0.1 ? '~' : 'ns';
      const [w1, w2, w3] = e.tg.split('|');
      console.log(`    "${w1}  ${w2}  ${w3}"  enrich=${e.enrich.toFixed(1)}×  p=${pv <= 0 ? `<${1/N_PERMS}` : pv.toFixed(3)} ${sig}`);
    }
  }

  // Cross-family check: do any top trigrams appear in multiple families?
  console.log('\n\n═══ CROSS-FAMILY CHECK ═══\n');
  console.log('Top trigrams for each family and how often they appear in the others:\n');
  for (const fam of FAMILIES) {
    const famFolios = folios.filter(f => f.family === fam);
    const famTgFreq = new Map<string, number>();
    for (const f of famFolios) for (const tg of f.tgSet) famTgFreq.set(tg, (famTgFreq.get(tg) ?? 0) + 1);
    const top3 = [...famTgFreq.entries()]
      .filter(([tg, k]) => k >= 2)
      .map(([tg, k]) => ({ tg, k, enrich: (k / famFolios.length) / ((tgFolioFreq.get(tg) ?? 0) / allN) }))
      .sort((a, b) => b.enrich - a.enrich)
      .slice(0, 3);
    for (const e of top3) {
      const [w1, w2, w3] = e.tg.split('|');
      const counts = FAMILIES.map(f2 => {
        const n = folios.filter(f => f.family === f2 && f.tgSet.has(e.tg)).length;
        return `${f2.slice(0,3)}:${n}`;
      }).join('  ');
      console.log(`  ${fam.padEnd(12)} "${w1}  ${w2}  ${w3}"  [${counts}]`);
    }
  }

  console.log('\nDone.');
}
main().catch(e => { console.error(e); process.exit(1); });
