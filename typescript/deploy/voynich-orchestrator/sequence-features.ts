// Sequence-level analysis: bigram Jaccard clustering + bigram entropy as family feature
// Tests whether word *order* (not just word rates) encodes botanical family membership.
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES, rankBiserial, shuffle } from './stat-types.ts';

const N_PERMS = 2000;

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

function bigramJaccard(a: string[], b: string[]): number {
  const setA = new Set(a), setB = new Set(b);
  let inter = 0;
  for (const bg of setA) if (setB.has(bg)) inter++;
  return inter / (setA.size + setB.size - inter);
}

function unigramJaccard(a: string[], b: string[]): number {
  const setA = new Set(a), setB = new Set(b);
  let inter = 0;
  for (const w of setA) if (setB.has(w)) inter++;
  return inter / (setA.size + setB.size - inter);
}

function entropy(tokens: string[]): number {
  const n = tokens.length;
  if (n === 0) return 0;
  const freq = new Map<string, number>();
  for (const t of tokens) freq.set(t, (freq.get(t) ?? 0) + 1);
  let h = 0;
  for (const c of freq.values()) { const p = c / n; h -= p * Math.log2(p); }
  return h;
}

function permTest(groupA: number[], groupB: number[]): { r: number; p: number } {
  const r = rankBiserial(groupA, groupB);
  const all = [...groupA, ...groupB]; const nA = groupA.length;
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

  // Build per-folio word sequences and derived structures
  type FolioData = { id: string; family: string; words: string[]; bgs: string[] };
  const folios: FolioData[] = [];
  for (const r of eRows) {
    if (!r.folio_id || !r.eva_text) continue;
    const fam = familyMap.get(r.folio_id) ?? '';
    if (!BOTANICAL_FAMILIES.has(fam)) continue;
    const words = extractEvaWords(r.eva_text);
    if (words.length < 20) continue;
    folios.push({ id: r.folio_id, family: fam, words, bgs: bigrams(words) });
  }

  const FAMILIES = ['solanaceae', 'thistle', 'plantago', 'poppy', 'lily-family', 'artemisia'];

  // ── Part 1: Bigram vs Unigram Jaccard clustering ──────────────────────────
  console.log('=== Part 1: Bigram vs Unigram Jaccard clustering ===\n');

  let withinBg = 0, betweenBg = 0, withinUni = 0, betweenUni = 0;
  let nWithin = 0, nBetween = 0;

  for (let i = 0; i < folios.length; i++) {
    for (let j = i + 1; j < folios.length; j++) {
      const fi = folios[i], fj = folios[j];
      const bg = bigramJaccard(fi.bgs, fj.bgs);
      const uni = unigramJaccard(fi.words, fj.words);
      if (fi.family === fj.family) {
        withinBg += bg; withinUni += uni; nWithin++;
      } else {
        betweenBg += bg; betweenUni += uni; nBetween++;
      }
    }
  }

  const wBg = withinBg / nWithin, bBg = betweenBg / nBetween;
  const wUni = withinUni / nWithin, bUni = betweenUni / nBetween;
  console.log(`Unigram Jaccard:  within=${wUni.toFixed(4)}  between=${bUni.toFixed(4)}  lift=${(wUni/bUni).toFixed(3)}×`);
  console.log(`Bigram  Jaccard:  within=${wBg.toFixed(4)}  between=${bBg.toFixed(4)}  lift=${(wBg/bBg).toFixed(3)}×`);
  console.log(`\nBigram lift ratio vs unigram: ${((wBg/bBg)/(wUni/bUni)).toFixed(3)}×`);
  console.log('(>1.0 = bigrams cluster more tightly by family than unigrams)\n');

  // ── Part 2: Bigram entropy and type/token ratio as family features ─────────
  console.log('=== Part 2: Sequence features as family discriminators ===\n');

  // Collect per-folio scalar values
  const byFamily: Record<string, { bgEntropy: number[]; bgTypeToken: number[]; uniEntropy: number[] }> = {};
  const allBot = { bgEntropy: [] as number[], bgTypeToken: [] as number[], uniEntropy: [] as number[] };

  for (const f of folios) {
    const bgEnt = entropy(f.bgs);
    const bgTT = new Set(f.bgs).size / f.bgs.length;
    const uniEnt = entropy(f.words);
    if (!byFamily[f.family]) byFamily[f.family] = { bgEntropy: [], bgTypeToken: [], uniEntropy: [] };
    byFamily[f.family].bgEntropy.push(bgEnt);
    byFamily[f.family].bgTypeToken.push(bgTT);
    byFamily[f.family].uniEntropy.push(uniEnt);
    allBot.bgEntropy.push(bgEnt);
    allBot.bgTypeToken.push(bgTT);
    allBot.uniEntropy.push(uniEnt);
  }

  const SEQUENCE_FEATS: Array<[string, keyof typeof allBot]> = [
    ['bigram entropy', 'bgEntropy'],
    ['bigram type/token ratio', 'bgTypeToken'],
    ['unigram entropy (reference)', 'uniEntropy'],
  ];

  for (const [label, key] of SEQUENCE_FEATS) {
    console.log(`--- ${label} ---`);
    const sig: string[] = [], border: string[] = [], nulls: string[] = [];
    for (const fam of FAMILIES) {
      const groupA = byFamily[fam]?.[key] ?? [];
      if (groupA.length < 3) continue;
      const { r, p } = permTest(groupA, allBot[key]);
      const mark = p < 0.001 ? '***' : p < 0.01 ? '**' : p < 0.05 ? '*' : p < 0.1 ? '~' : 'ns';
      const line = `  ${fam}(n=${groupA.length}) r=${r.toFixed(3)} p=${p.toFixed(3)} ${mark}`;
      if (p < 0.05) sig.push(line);
      else if (p < 0.10) border.push(line);
      else nulls.push(line);
    }
    for (const l of [...sig, ...border, ...nulls]) console.log(l);
    console.log();
  }

  // ── Part 3: Characteristic bigrams — family-specific word pairs ───────────
  console.log('=== Part 3: Top characteristic bigrams per family ===\n');
  console.log('(bigrams appearing in ≥3 folios of target family; enrichment vs. all-botanical)\n');

  const allFolioCount = folios.length;
  const bgFolioFreq = new Map<string, number>(); // how many folios contain each bigram
  for (const f of folios) for (const bg of new Set(f.bgs)) bgFolioFreq.set(bg, (bgFolioFreq.get(bg) ?? 0) + 1);

  for (const fam of ['solanaceae', 'thistle', 'plantago']) {
    const famFolios = folios.filter(f => f.family === fam);
    const famBgFreq = new Map<string, number>();
    for (const f of famFolios) for (const bg of new Set(f.bgs)) famBgFreq.set(bg, (famBgFreq.get(bg) ?? 0) + 1);

    // Enrichment: rate in family vs rate in all-botanical
    type Entry = { bg: string; famRate: number; allRate: number; enrichment: number; k: number };
    const entries: Entry[] = [];
    for (const [bg, k] of famBgFreq) {
      if (k < 3) continue;
      const famRate = k / famFolios.length;
      const allRate = (bgFolioFreq.get(bg) ?? 0) / allFolioCount;
      if (allRate === 0) continue;
      entries.push({ bg, famRate, allRate, enrichment: famRate / allRate, k });
    }
    entries.sort((a, b) => b.enrichment - a.enrichment);

    console.log(`${fam} (top 8 by enrichment):`);
    for (const e of entries.slice(0, 8)) {
      console.log(`  "${e.bg.replace('|', ' → ')}"  k=${e.k}  famRate=${e.famRate.toFixed(3)}  allRate=${e.allRate.toFixed(3)}  enrich=${e.enrichment.toFixed(2)}×`);
    }
    console.log();
  }

  console.log('Done.');
}
main().catch(e => { console.error(e); process.exit(1); });
