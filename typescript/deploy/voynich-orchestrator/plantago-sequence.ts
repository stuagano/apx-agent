// Plantago sequence investigation:
// 1. Does plantago's 90% bigram accuracy come from absence of other families' bigrams,
//    or from its own distinctive sequences?
// 2. What are plantago's most enriched bigrams at lower thresholds (k>=2)?
// 3. What transitions surround plantago's known morphological markers (ch-init, -chy words)?
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES, shuffle } from './stat-types.ts';

async function sql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ warehouse_id: wid, statement: s, wait_timeout: '50s' }) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns ?? []).map((c: any) => c.name);
  return (d.result?.data_array ?? []).map((row: any[]) => Object.fromEntries(cols.map((c: string, i: number) => [c, row[i]])));
}

function bgs(words: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < words.length - 1; i++) out.push(`${words[i]}|${words[i + 1]}`);
  return out;
}

function euclidean(a: number[], b: number[]): number {
  return Math.sqrt(a.reduce((s, v, i) => s + (v - b[i]) ** 2, 0));
}

async function main() {
  const vRows = await sql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const eRows = await sql("SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section='herbal'");

  const familyMap = new Map<string, string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));

  type Folio = { id: string; family: string; words: string[]; bgList: string[]; bgSet: Set<string> };
  const folios: Folio[] = [];
  for (const r of eRows) {
    if (!r.folio_id || !r.eva_text) continue;
    const fam = familyMap.get(r.folio_id) ?? '';
    if (!BOTANICAL_FAMILIES.has(fam)) continue;
    const words = extractEvaWords(r.eva_text);
    if (words.length < 20) continue;
    const bgList = bgs(words);
    folios.push({ id: r.folio_id, family: fam, words, bgList, bgSet: new Set(bgList) });
  }

  const FAMILIES = ['solanaceae', 'thistle', 'plantago', 'poppy'];
  const byFam = Object.fromEntries(FAMILIES.map(f => [f, folios.filter(x => x.family === f)]));
  const allN = folios.length;

  // Global bigram folio frequency
  const bgFolioFreq = new Map<string, number>();
  for (const f of folios) for (const bg of f.bgSet) bgFolioFreq.set(bg, (bgFolioFreq.get(bg) ?? 0) + 1);

  // ── Part 1: Plantago's own bigrams (k>=2, all enrichment levels) ──────────
  console.log('=== Part 1: Plantago characteristic bigrams (k≥2) ===\n');
  const pla = byFam['plantago'];
  const plaBgFreq = new Map<string, number>();
  for (const f of pla) for (const bg of f.bgSet) plaBgFreq.set(bg, (plaBgFreq.get(bg) ?? 0) + 1);

  type Entry = { bg: string; k: number; plaRate: number; allRate: number; enrich: number };
  const plaEntries: Entry[] = [];
  for (const [bg, k] of plaBgFreq) {
    if (k < 2) continue;
    const plaRate = k / pla.length;
    const allRate = (bgFolioFreq.get(bg) ?? 0) / allN;
    plaEntries.push({ bg, k, plaRate, allRate, enrich: allRate > 0 ? plaRate / allRate : Infinity });
  }
  plaEntries.sort((a, b) => b.enrich - a.enrich);
  console.log(`Total bigrams appearing in ≥2 plantago folios: ${plaEntries.length}`);
  console.log('\nTop 20 by enrichment:');
  for (const e of plaEntries.slice(0, 20)) {
    const [w1, w2] = e.bg.split('|');
    console.log(`  "${w1} → ${w2}"  k=${e.k}/10  plaRate=${e.plaRate.toFixed(2)}  allRate=${e.allRate.toFixed(3)}  enrich=${e.enrich.toFixed(2)}×`);
  }

  // ── Part 2: Do plantago folios score LOW on other families' bigrams? ───────
  console.log('\n=== Part 2: Mean bigram-feature score by family ===\n');
  console.log('(how many of the 31 discriminative bigrams does each folio contain on average?)\n');

  // Reconstruct the 31 discriminative bigrams from bigram-classifier.ts logic
  const discBgs: string[] = [];
  for (const fam of FAMILIES) {
    const famFolios = byFam[fam];
    const famBgFreq = new Map<string, number>();
    for (const f of famFolios) for (const bg of f.bgSet) famBgFreq.set(bg, (famBgFreq.get(bg) ?? 0) + 1);
    for (const [bg, k] of famBgFreq) {
      if (k < 3) continue;
      const famRate = k / famFolios.length;
      const allRate = (bgFolioFreq.get(bg) ?? 0) / allN;
      if (allRate > 0 && famRate / allRate >= 2.0) discBgs.push(bg);
    }
  }
  const discSet = [...new Set(discBgs)];

  // Which family does each discriminative bigram belong to?
  const bgOrigin = new Map<string, string>();
  for (const fam of FAMILIES) {
    const famFolios = byFam[fam];
    const famBgFreq = new Map<string, number>();
    for (const f of famFolios) for (const bg of f.bgSet) famBgFreq.set(bg, (famBgFreq.get(bg) ?? 0) + 1);
    for (const bg of discSet) {
      const k = famBgFreq.get(bg) ?? 0;
      if (k < 3) continue;
      const famRate = k / famFolios.length;
      const allRate = (bgFolioFreq.get(bg) ?? 0) / allN;
      if (allRate > 0 && famRate / allRate >= 2.0) bgOrigin.set(bg, fam);
    }
  }

  console.log(`Discriminative bigrams by origin family:`);
  for (const fam of FAMILIES) {
    const owned = discSet.filter(bg => bgOrigin.get(bg) === fam);
    console.log(`  ${fam}: ${owned.length} bigrams`);
  }

  console.log('\nMean number of discriminative bigrams per folio (by family):');
  for (const fam of FAMILIES) {
    const vals = byFam[fam].map(f => discSet.filter(bg => f.bgSet.has(bg)).length);
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const breakdown = FAMILIES.map(src => {
      const srcBgs = discSet.filter(bg => bgOrigin.get(bg) === src);
      const m = byFam[fam].map(f => srcBgs.filter(bg => f.bgSet.has(bg)).length).reduce((a,b)=>a+b,0) / byFam[fam].length;
      return `${src.slice(0,3)}-bgs:${m.toFixed(1)}`;
    });
    console.log(`  ${fam.padEnd(12)} total=${mean.toFixed(1)}  [${breakdown.join('  ')}]`);
  }

  // ── Part 3: Ablation — classifier using only plantago's OWN bigrams ────────
  console.log('\n=== Part 3: Ablation — classify using plantago-positive bigrams only ===\n');

  // Plantago bigrams: k>=2, enrichment>=1.5
  const plaBgs15 = plaEntries.filter(e => e.enrich >= 1.5).map(e => e.bg);
  console.log(`Plantago bigrams (k≥2, enrich≥1.5): ${plaBgs15.length}`);

  if (plaBgs15.length > 0) {
    const encode = (f: Folio, feats: string[]) => feats.map(bg => f.bgSet.has(bg) ? 1 : 0);
    const data = folios.filter(f => FAMILIES.includes(f.family) && byFam[f.family].length >= 5);

    function looRun(feats: string[]): { acc: number; plaAcc: number } {
      let correct = 0; let plaC = 0, plaT = 0;
      for (let i = 0; i < data.length; i++) {
        const held = data[i];
        const rest = data.filter((_, j) => j !== i);
        const centroids: Record<string, number[]> = {};
        for (const fam of FAMILIES) {
          const members = rest.filter(d => d.family === fam);
          if (!members.length) continue;
          centroids[fam] = feats.map(bg => members.filter(d => d.bgSet.has(bg)).length / members.length);
        }
        const heldVec = encode(held, feats);
        let best = '', bestD = Infinity;
        for (const [fam, c] of Object.entries(centroids)) {
          const d = euclidean(heldVec, c);
          if (d < bestD) { bestD = d; best = fam; }
        }
        if (best === held.family) correct++;
        if (held.family === 'plantago') { plaT++; if (best === 'plantago') plaC++; }
      }
      return { acc: correct / data.length, plaAcc: plaC / plaT };
    }

    const r15 = looRun(plaBgs15);
    console.log(`Plantago-only bigrams (${plaBgs15.length}): overall=${(r15.acc*100).toFixed(1)}%  plantago=${(r15.plaAcc*100).toFixed(1)}%`);
  } else {
    console.log('No plantago bigrams meet threshold — plantago has no positive sequence identity at k≥2, enrich≥1.5.');
  }

  // ── Part 4: Transitions around plantago morphological markers ─────────────
  console.log('\n=== Part 4: What follows/precedes ch-init and -chy words in plantago? ===\n');

  for (const fam of ['plantago', 'solanaceae']) {
    const famFolios = byFam[fam];
    const chInitSuccessors = new Map<string, number>();
    const chyPredecessors = new Map<string, number>();
    for (const f of famFolios) {
      for (let i = 0; i < f.words.length; i++) {
        if (f.words[i].startsWith('ch') && i + 1 < f.words.length)
          chInitSuccessors.set(f.words[i + 1], (chInitSuccessors.get(f.words[i + 1]) ?? 0) + 1);
        if (f.words[i].endsWith('chy') && i > 0)
          chyPredecessors.set(f.words[i - 1], (chyPredecessors.get(f.words[i - 1]) ?? 0) + 1);
      }
    }
    const topSucc = [...chInitSuccessors.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
    const topPred = [...chyPredecessors.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8);
    console.log(`${fam} — top successors after ch-init words: ${topSucc.map(([w,c])=>`${w}(${c})`).join('  ')}`);
    console.log(`${fam} — top predecessors before -chy words: ${topPred.map(([w,c])=>`${w}(${c})`).join('  ')}`);
    console.log();
  }

  console.log('Done.');
}
main().catch(e => { console.error(e); process.exit(1); });
