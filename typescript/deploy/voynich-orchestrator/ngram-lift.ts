// N-gram Jaccard lift curve: unigram → bigram → trigram → 4-gram
// Tests whether family clustering signal increases with n-gram order or plateaus.
import { resolveHost, resolveToken } from './appkit-agent/index.mjs';
import { extractEvaWords, classifyPlantFamily, BOTANICAL_FAMILIES, shuffle } from './stat-types.ts';

const N_PERMS = 1000;

async function sql(s: string) {
  const host = resolveHost(); const token = await resolveToken(); const wid = process.env.DATABRICKS_WAREHOUSE_ID;
  const res = await fetch(`${host}/api/2.0/sql/statements`, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ warehouse_id: wid, statement: s, wait_timeout: '50s' }) });
  const d = await res.json() as any;
  const cols = (d.manifest?.schema?.columns ?? []).map((c: any) => c.name);
  return (d.result?.data_array ?? []).map((row: any[]) => Object.fromEntries(cols.map((c: string, i: number) => [c, row[i]])));
}

function ngrams(words: string[], n: number): string[] {
  const out: string[] = [];
  for (let i = 0; i <= words.length - n; i++) out.push(words.slice(i, i + n).join('|'));
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
  return nW > 0 && nB > 0 ? (within / nW) / (between / nB) : 1;
}

async function main() {
  const vRows = await sql("SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section='herbal'");
  const eRows = await sql("SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section='herbal'");

  const familyMap = new Map<string, string>();
  for (const r of vRows) if (r.folio_id && r.subject_candidates) familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates));

  const folios: Array<{ id: string; family: string; words: string[] }> = [];
  for (const r of eRows) {
    if (!r.folio_id || !r.eva_text) continue;
    const fam = familyMap.get(r.folio_id) ?? '';
    if (!BOTANICAL_FAMILIES.has(fam)) continue;
    const words = extractEvaWords(r.eva_text);
    if (words.length < 20) continue;
    folios.push({ id: r.folio_id, family: fam, words });
  }

  const families = folios.map(f => f.family);
  console.log(`Folios: ${folios.length}  |  N_PERMS: ${N_PERMS}\n`);
  console.log('=== N-gram Jaccard lift curve ===\n');
  console.log(`${'n'.padEnd(3)}  ${'within'.padEnd(8)}  ${'between'.padEnd(8)}  ${'lift'.padEnd(8)}  ${'lift/unigram'.padEnd(12)}  p-value`);
  console.log('─'.repeat(65));

  let unigramLift = 1;
  for (const n of [1, 2, 3, 4]) {
    const tokenized = folios.map(f => ({ family: f.family, tokens: ngrams(f.words, n) }));

    // Observed lift
    let within = 0, between = 0, nW = 0, nB = 0;
    for (let i = 0; i < tokenized.length; i++) {
      for (let j = i + 1; j < tokenized.length; j++) {
        const j12 = jaccard(tokenized[i].tokens, tokenized[j].tokens);
        if (tokenized[i].family === tokenized[j].family) { within += j12; nW++; }
        else { between += j12; nB++; }
      }
    }
    const withinMean = within / nW, betweenMean = between / nB;
    const observedLift = withinMean / betweenMean;
    if (n === 1) unigramLift = observedLift;

    // Permutation test
    let extreme = 0;
    for (let p = 0; p < N_PERMS; p++) {
      const shuffled = shuffle(families);
      const permLift = computeLift(tokenized.map((f, i) => ({ family: shuffled[i], tokens: f.tokens })));
      if (permLift >= observedLift) extreme++;
    }
    const pVal = extreme / N_PERMS;
    const pStr = pVal === 0 ? `<${1/N_PERMS}` : pVal.toFixed(3);
    const sig = pVal < 0.001 ? '***' : pVal < 0.01 ? '**' : pVal < 0.05 ? '*' : pVal < 0.1 ? '~' : 'ns';

    console.log(
      `${String(n).padEnd(3)}  ${withinMean.toFixed(5).padEnd(8)}  ${betweenMean.toFixed(5).padEnd(8)}  ` +
      `${observedLift.toFixed(3).padEnd(8)}  ${(observedLift / unigramLift).toFixed(3).padEnd(12)}  ${pStr} ${sig}`
    );
  }

  // Per-family breakdown at each n
  console.log('\n=== Per-family within-vs-between lift by n-gram order ===\n');
  const FAMILIES = ['solanaceae', 'thistle', 'plantago', 'poppy'];
  for (const n of [1, 2, 3]) {
    console.log(`n=${n}:`);
    for (const fam of FAMILIES) {
      const tokenized = folios.map(f => ({ family: f.family, tokens: ngrams(f.words, n) }));
      let wFam = 0, bFam = 0, nWF = 0, nBF = 0;
      for (let i = 0; i < tokenized.length; i++) {
        if (tokenized[i].family !== fam) continue;
        for (let j = 0; j < tokenized.length; j++) {
          if (i === j) continue;
          const j12 = jaccard(tokenized[i].tokens, tokenized[j].tokens);
          if (tokenized[j].family === fam) { wFam += j12; nWF++; }
          else { bFam += j12; nBF++; }
        }
      }
      if (nWF === 0 || nBF === 0) continue;
      const lift = (wFam / nWF) / (bFam / nBF);
      console.log(`  ${fam.padEnd(12)} within=${( wFam/nWF).toFixed(5)}  between=${( bFam/nBF).toFixed(5)}  lift=${lift.toFixed(3)}×`);
    }
  }

  console.log('\nDone.');
}
main().catch(e => { console.error(e); process.exit(1); });
