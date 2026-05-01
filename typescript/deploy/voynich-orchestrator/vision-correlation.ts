/**
 * Vision-EVA correlation analysis.
 *
 * Tests whether EVA word frequency profiles cluster by visually-identified
 * plant type. If the manuscript has semantic content, folios depicting the
 * same plant should share EVA vocabulary (a word for "root" should appear
 * consistently on root-heavy folios, etc.). If it's Rugg-style generation,
 * EVA word distributions should be independent of visual content.
 *
 * Two tests:
 *
 *   VOCABULARY OVERLAP TEST: For each plant-type group (≥3 folios),
 *     compute pairwise Jaccard similarity within the group and compare
 *     against pairwise similarity between random folio pairs. A statistically
 *     higher within-group similarity would be evidence of plant-specific
 *     EVA vocabulary.
 *
 *   LLM COHERENCE TEST: Feed visual_description + a sample of EVA words
 *     (most distinctive words for that folio vs. the corpus) to an LLM.
 *     Ask: "Do you see any pattern in these EVA word sequences that
 *     correlates with the plant described?" This is deliberately open-ended —
 *     we're not asking the LLM to decode, just to notice structure.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx vision-correlation.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

// ---------------------------------------------------------------------------
// SQL helper
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '30s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?: { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?: { state?: string; error?: { message?: string } };
  };
  if (data.status?.state === 'FAILED') throw new Error(`SQL failed: ${data.status.error?.message}`);
  const cols = (data.manifest?.schema?.columns ?? []).map((c) => c.name);
  return (data.result?.data_array ?? []).map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// FM API (LLM calls via Databricks FMAPI)
// ---------------------------------------------------------------------------

async function callFMAPI(prompt: string): Promise<string> {
  const host = resolveHost();
  const token = await resolveToken();
  const model = process.env.JUDGE_MODEL ?? 'databricks-claude-sonnet-4-6';
  const res = await fetch(`${host}/serving-endpoints/${model}/invocations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ messages: [{ role: 'user', content: prompt }], max_tokens: 600 }),
  });
  if (!res.ok) throw new Error(`FMAPI ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
  return data.choices?.[0]?.message?.content ?? '';
}

// ---------------------------------------------------------------------------
// EVA word extraction (single-char tokenization — consistent with other scripts)
// ---------------------------------------------------------------------------

function extractEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

// ---------------------------------------------------------------------------
// Similarity metrics
// ---------------------------------------------------------------------------

function jaccardSimilarity(setA: Set<string>, setB: Set<string>): number {
  if (setA.size === 0 && setB.size === 0) return 1;
  let inter = 0;
  for (const w of setA) if (setB.has(w)) inter++;
  const union = setA.size + setB.size - inter;
  return union === 0 ? 0 : inter / union;
}

/** TF-IDF-weighted cosine similarity between two frequency maps. */
function tfidfCosine(
  freqA: Map<string, number>, totalA: number,
  freqB: Map<string, number>, totalB: number,
  idf: Map<string, number>,
): number {
  const vocab = new Set([...freqA.keys(), ...freqB.keys()]);
  let dotProduct = 0, normA = 0, normB = 0;
  for (const w of vocab) {
    const idfVal = idf.get(w) ?? 0;
    const tA = ((freqA.get(w) ?? 0) / totalA) * idfVal;
    const tB = ((freqB.get(w) ?? 0) / totalB) * idfVal;
    dotProduct += tA * tB;
    normA += tA * tA;
    normB += tB * tB;
  }
  return normA === 0 || normB === 0 ? 0 : dotProduct / (Math.sqrt(normA) * Math.sqrt(normB));
}

// ---------------------------------------------------------------------------
// Plant family grouping (from visual subject candidates)
// ---------------------------------------------------------------------------

function classifyPlantFamily(subjectCandidates: string): string {
  let candidates: Array<{ name: string; latin: string; confidence: number }> = [];
  try { candidates = JSON.parse(subjectCandidates); } catch { return 'unknown'; }
  const top = candidates[0];
  if (!top || top.confidence < 0.35) return 'uncertain';

  const latin = (top.latin ?? '').toLowerCase();
  const name  = (top.name ?? '').toLowerCase();

  // Broad families based on keywords in the identification
  if (/papaver|poppy/.test(latin + name)) return 'poppy';
  if (/rosa|rose/.test(latin + name)) return 'rose';
  if (/mentha|mint|melissa|melissa/.test(latin + name)) return 'mint-family';
  if (/salvia|sage/.test(latin + name)) return 'mint-family';
  if (/thymus|thyme|origanum|oregano|rosmarinus|rosemary|lavandula/.test(latin + name)) return 'mint-family';
  if (/cirsium|carduus|thistle|carlina|centaurea/.test(latin + name)) return 'thistle';
  if (/helleborus|aconitum|ranunculus|clematis|anemone/.test(latin + name)) return 'ranunculaceae';
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus|datura/.test(latin + name)) return 'solanaceae';
  if (/foeniculum|apium|petroselinum|coriandrum|anethum|daucus/.test(latin + name)) return 'apiaceae';
  if (/plantago|plantain/.test(latin + name)) return 'plantago';
  if (/verbena/.test(latin + name)) return 'verbena';
  if (/artemisia|absinthium|wormwood/.test(latin + name)) return 'artemisia';
  if (/nasturtium|tropaeolum|brassica|cress/.test(latin + name)) return 'brassicaceae';
  if (/iris|lily|lilium|crocus/.test(latin + name)) return 'lily-family';

  // Fall back to genus of the Latin binomial
  const genus = latin.split(/[\s/,]/)[0];
  return genus || 'unknown';
}

// ---------------------------------------------------------------------------
// Distinctive words: TF-IDF highlighting words that appear more on this folio
// than in the corpus average.
// ---------------------------------------------------------------------------

function distinctiveWords(
  folioFreq: Map<string, number>, folioTotal: number,
  corpusFreq: Map<string, number>, corpusTotal: number,
  topN: number,
): string[] {
  const scored: Array<[string, number]> = [];
  for (const [w, count] of folioFreq) {
    const tf = count / folioTotal;
    const corpusTf = (corpusFreq.get(w) ?? 0) / corpusTotal;
    const excess = tf - corpusTf;
    scored.push([w, excess]);
  }
  return scored.sort((a, b) => b[1] - a[1]).slice(0, topN).map(([w]) => w);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('vision-correlation — EVA vocabulary vs. visual plant identification');
  console.log('');

  // Load vision data
  console.log('Loading folio_vision_analysis...');
  const visionRows = await executeSql(`
    SELECT folio_id, subject_candidates, botanical_features, visual_description
    FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    WHERE section = 'herbal'
    ORDER BY folio_id
  `);

  // Load EVA corpus
  console.log('Loading EVA corpus...');
  const evaRows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
  `);

  const evaByFolio = new Map<string, string>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaByFolio.set(r.folio_id, r.eva_text);
  }

  console.log(`  ${visionRows.length} vision folios, ${evaByFolio.size} EVA folios`);

  // Build per-folio word frequency profiles
  interface FolioProfile {
    folioId: string;
    family: string;
    plantName: string;
    confidence: number;
    visualDescription: string;
    words: string[];
    freq: Map<string, number>;
    typeSet: Set<string>;
  }

  const profiles: FolioProfile[] = [];
  const corpusFreq = new Map<string, number>();
  let corpusTotal = 0;

  for (const r of visionRows) {
    const folioId = r.folio_id ?? '';
    const evaText = evaByFolio.get(folioId) ?? '';
    if (!evaText) continue;

    const words = extractEvaWords(evaText);
    if (words.length < 10) continue;

    const freq = new Map<string, number>();
    for (const w of words) {
      freq.set(w, (freq.get(w) ?? 0) + 1);
      corpusFreq.set(w, (corpusFreq.get(w) ?? 0) + 1);
      corpusTotal++;
    }

    let candidates: Array<{ name: string; confidence: number }> = [];
    try { candidates = JSON.parse(r.subject_candidates ?? '[]'); } catch { /* ok */ }
    const top = candidates[0] ?? { name: 'unknown', confidence: 0 };

    profiles.push({
      folioId,
      family: classifyPlantFamily(r.subject_candidates ?? '[]'),
      plantName: top.name ?? 'unknown',
      confidence: top.confidence ?? 0,
      visualDescription: r.visual_description ?? '',
      words,
      freq,
      typeSet: new Set(freq.keys()),
    });
  }

  console.log(`  ${profiles.length} folios with both vision + EVA data`);

  // Build IDF from corpus
  const docFreq = new Map<string, number>();
  const N = profiles.length;
  for (const p of profiles) for (const w of p.typeSet) docFreq.set(w, (docFreq.get(w) ?? 0) + 1);
  const idf = new Map<string, number>();
  for (const [w, df] of docFreq) idf.set(w, Math.log((N + 1) / (df + 1)));

  // ---------------------------------------------------------------------------
  // TEST 1: Within-group vs. between-group Jaccard similarity
  // ---------------------------------------------------------------------------
  console.log('\n=== TEST 1: EVA vocabulary overlap by plant family ===');
  console.log('');

  const byFamily = new Map<string, FolioProfile[]>();
  for (const p of profiles) {
    if (!byFamily.has(p.family)) byFamily.set(p.family, []);
    byFamily.get(p.family)!.push(p);
  }

  // Show family distribution
  const sortedFamilies = [...byFamily.entries()].sort((a, b) => b[1].length - a[1].length);
  console.log('  Plant families identified:');
  for (const [fam, fps] of sortedFamilies.slice(0, 12)) {
    console.log(`    ${fam.padEnd(20)} ${fps.length} folios  (e.g. ${fps.slice(0, 2).map(f => f.folioId).join(', ')})`);
  }

  // Compute within-group Jaccard for each family with ≥3 members
  const withinSims: number[] = [];
  const withinByFamily: Array<{ family: string; n: number; meanJaccard: number; meanCosine: number }> = [];

  for (const [fam, fps] of byFamily) {
    if (fps.length < 3) continue;
    const jaccards: number[] = [];
    const cosines: number[] = [];
    for (let i = 0; i < fps.length; i++) {
      for (let j = i + 1; j < fps.length; j++) {
        jaccards.push(jaccardSimilarity(fps[i].typeSet, fps[j].typeSet));
        cosines.push(tfidfCosine(fps[i].freq, fps[i].words.length, fps[j].freq, fps[j].words.length, idf));
      }
    }
    const meanJ = jaccards.reduce((s, x) => s + x, 0) / jaccards.length;
    const meanC = cosines.reduce((s, x) => s + x, 0) / cosines.length;
    withinSims.push(...jaccards);
    withinByFamily.push({ family: fam, n: fps.length, meanJaccard: meanJ, meanCosine: meanC });
  }

  // Compute between-group Jaccard (random pairs from different families)
  const betweenSims: number[] = [];
  const allProfiles = [...profiles];
  const rng = { v: 42 };
  const rand = () => { rng.v = (rng.v * 1664525 + 1013904223) & 0x7fffffff; return rng.v / 0x7fffffff; };
  for (let trial = 0; trial < 500; trial++) {
    const i = Math.floor(rand() * allProfiles.length);
    let j = Math.floor(rand() * allProfiles.length);
    while (j === i || allProfiles[i].family === allProfiles[j].family) j = Math.floor(rand() * allProfiles.length);
    betweenSims.push(jaccardSimilarity(allProfiles[i].typeSet, allProfiles[j].typeSet));
  }

  const meanWithin = withinSims.length > 0 ? withinSims.reduce((s, x) => s + x, 0) / withinSims.length : 0;
  const meanBetween = betweenSims.reduce((s, x) => s + x, 0) / betweenSims.length;

  console.log('\n  Within-group vs. between-group Jaccard similarity:');
  console.log(`    within-group  (same family): ${meanWithin.toFixed(4)}  (n=${withinSims.length} pairs)`);
  console.log(`    between-group (diff family): ${meanBetween.toFixed(4)}  (n=${betweenSims.length} pairs)`);
  const lift = meanWithin / meanBetween;
  console.log(`    lift: ${lift.toFixed(3)}x  ${lift > 1.10 ? '← within > between' : lift < 0.95 ? '← between ≥ within (no clustering)' : '← within ≈ between (no clustering)'}`);

  console.log('\n  Per-family breakdown (families with ≥3 folios):');
  console.log('  ' + 'family'.padEnd(20) + 'n'.padStart(4) + 'Jaccard'.padStart(10) + 'TF-IDF cos'.padStart(12));
  console.log('  ' + '-'.repeat(48));
  for (const r of withinByFamily.sort((a, b) => b.meanJaccard - a.meanJaccard)) {
    console.log(`  ${r.family.padEnd(20)}${r.n.toString().padStart(4)}${r.meanJaccard.toFixed(4).padStart(10)}${r.meanCosine.toFixed(4).padStart(12)}`);
  }

  // ---------------------------------------------------------------------------
  // TEST 2: LLM coherence — do distinctive EVA words correlate with visual content?
  // ---------------------------------------------------------------------------
  const runLLM = process.env.SKIP_LLM !== '1';
  if (runLLM) {
    console.log('\n=== TEST 2: LLM coherence — distinctive EVA words vs. visual description ===');
    console.log('(Asking an LLM to look for pattern between EVA word shape and visual content)');
    console.log('');

    // Pick one folio from each of the top botanical families with high confidence.
    // Exclude non-botanical families ('n', 'non-botanical') which sort to the top by confidence
    // but are zodiac/astronomical diagrams — not useful for content-word correlation.
    const NON_BOTANICAL = new Set(['n', 'non-botanical', 'uncertain', 'unknown']);
    const seenFamilies = new Set<string>();
    const candidates = profiles
      .filter((p) => p.confidence >= 0.45 && !NON_BOTANICAL.has(p.family))
      .sort((a, b) => b.confidence - a.confidence)
      .filter((p) => !seenFamilies.has(p.family) && seenFamilies.add(p.family) !== undefined)
      .slice(0, 5);

    for (const p of candidates) {
      const distinctive = distinctiveWords(p.freq, p.words.length, corpusFreq, corpusTotal, 20);
      const sampleLines = p.words.slice(0, 60).join(' ');

      const prompt = `You are analyzing the Voynich manuscript — a 15th-century illustrated herbal manuscript written in an unknown script called EVA (European Voynich Alphabet). The manuscript has never been decoded.

I'm going to show you:
1. A description of what the plant illustration on folio ${p.folioId} looks like
2. The 20 EVA words that appear most distinctively on this folio compared to the rest of the manuscript
3. A sample of the running EVA text on this folio

I am NOT asking you to decode the text. I am asking you to look for any structural or distributional patterns in the EVA word shapes that might correlate with the visual content — things like: are certain word shapes clustered at specific positions in the text? Do word lengths vary in a patterned way? Are any word shapes visually reminiscent of what's depicted?

Plant illustration (${p.plantName}, confidence ${(p.confidence * 100).toFixed(0)}%):
${p.visualDescription.slice(0, 400)}

Most distinctive EVA words on this folio (vs. corpus):
${distinctive.join('  ')}

Running EVA text sample (first 60 words):
${sampleLines}

In 3-4 sentences: what patterns, if any, do you notice? Be specific about what you observe, and honest about what could be coincidence.`;

      console.log(`  Folio ${p.folioId} (${p.plantName}, ${(p.confidence * 100).toFixed(0)}% confidence, family=${p.family}):`);
      console.log(`  Distinctive words: ${distinctive.slice(0, 10).join(', ')}`);
      try {
        const response = await callFMAPI(prompt);
        console.log(`  LLM response: ${response.trim().replace(/\n/g, '\n    ')}`);
      } catch (err) {
        console.log(`  LLM call failed: ${(err as Error).message}`);
      }
      console.log('');
    }
  }

  // ---------------------------------------------------------------------------
  // Verdict
  // ---------------------------------------------------------------------------
  console.log('=== VERDICT ===');
  if (lift > 1.15) {
    console.log(`  lift=${lift.toFixed(3)}x: within-group Jaccard EXCEEDS between-group.`);
    console.log('  EVA vocabulary clusters by plant type. This is evidence of semantic content —');
    console.log('  some EVA words appear preferentially on specific plant families.');
  } else if (lift > 1.05) {
    console.log(`  lift=${lift.toFixed(3)}x: weak within-group excess.`);
    console.log('  Possible but marginal clustering. Could be positional artifact (same folio → shared local words).');
  } else {
    console.log(`  lift=${lift.toFixed(3)}x: no vocabulary clustering by plant type.`);
    console.log('  EVA word distributions are independent of visually-identified plant family.');
    console.log('  Consistent with Rugg-style generation: word selection driven by position/grille,');
    console.log('  not by the semantic content of the illustrated plant.');
  }
}

main().catch((err) => {
  console.error('[vision-correlation] fatal:', err);
  process.exit(1);
});
