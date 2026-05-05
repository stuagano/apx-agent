/**
 * Spatial layout analysis for Voynich herbal folios.
 *
 * Uses the spatial_layout field in folio_vision_analysis (LLM-generated
 * text region positions) to investigate:
 *
 *   1. "Uncertain" anomaly — are the 33 low-confidence folios positionally
 *      clustered in the manuscript? (If yes, their EVA vocabulary similarity
 *      from vision-correlation is a quire artifact, not semantic structure.)
 *
 *   2. Layout-EVA correlation — do spatial layout features (number of text
 *      regions, label vs. description roles, text position) predict EVA word
 *      characteristics (word count, type diversity, avg word length)?
 *
 *   3. Section fingerprinting — do layout patterns differ systematically
 *      between herbal-proper folios and misclassified sections (zodiac,
 *      balneological, cosmological)?
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx spatial-layout-analysis.ts
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
// Types
// ---------------------------------------------------------------------------

interface TextRegion {
  position: string;     // e.g. "top-left", "left", "right", "bottom", "center"
  role: string;         // e.g. "label", "description", "unknown"
  estimated_lines: number;
}

interface SpatialLayout {
  text_regions: TextRegion[];
}

interface FolioData {
  folioId: string;
  subjectCandidates: string;
  spatialLayout: SpatialLayout | null;
  topName: string;
  topConfidence: number;
  family: string;
  // EVA stats
  wordCount: number;
  typeCount: number;
  avgWordLen: number;
  hapaxRate: number;
}

// ---------------------------------------------------------------------------
// Plant family classification (same logic as vision-correlation.ts)
// ---------------------------------------------------------------------------

function classifyPlantFamily(subjectCandidates: string): string {
  let candidates: Array<{ name: string; latin: string; confidence: number }> = [];
  try { candidates = JSON.parse(subjectCandidates); } catch { return 'unknown'; }
  const top = candidates[0];
  if (!top || top.confidence < 0.35) return 'uncertain';

  const latin = (top.latin ?? '').toLowerCase();
  const name  = (top.name ?? '').toLowerCase();

  if (/papaver|poppy/.test(latin + name)) return 'poppy';
  if (/rosa|rose/.test(latin + name)) return 'rose';
  if (/mentha|mint|melissa|salvia|sage|thymus|thyme|origanum|oregano|rosmarinus|rosemary|lavandula/.test(latin + name)) return 'mint-family';
  if (/cirsium|carduus|thistle|carlina|centaurea/.test(latin + name)) return 'thistle';
  if (/helleborus|aconitum|ranunculus|clematis|anemone/.test(latin + name)) return 'ranunculaceae';
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus|datura/.test(latin + name)) return 'solanaceae';
  if (/foeniculum|apium|petroselinum|coriandrum|anethum|daucus/.test(latin + name)) return 'apiaceae';
  if (/plantago|plantain/.test(latin + name)) return 'plantago';
  if (/verbena/.test(latin + name)) return 'verbena';
  if (/artemisia|absinthium|wormwood/.test(latin + name)) return 'artemisia';
  if (/nasturtium|tropaeolum|brassica|cress/.test(latin + name)) return 'brassicaceae';
  if (/iris|lily|lilium|crocus/.test(latin + name)) return 'lily-family';

  const genus = latin.split(/[\s/,]/)[0];
  return genus || 'unknown';
}

// ---------------------------------------------------------------------------
// Folio sort key — maps folio ID to a numeric position for manuscript ordering
// e.g. f1r → 1.0, f1v → 1.5, f10r → 10.0, f10r1 → 10.0, f10r2 → 10.1
// ---------------------------------------------------------------------------

function folioSortKey(id: string): number {
  const m = id.match(/^f(\d+)([rv])(\d*)$/i);
  if (!m) return 9999;
  const n = parseInt(m[1]);
  const rv = m[2].toLowerCase() === 'r' ? 0 : 0.5;
  const sub = m[3] ? parseInt(m[3]) * 0.1 : 0;
  return n + rv + sub;
}

// ---------------------------------------------------------------------------
// EVA word extraction
// ---------------------------------------------------------------------------

function extractEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('spatial-layout-analysis — text region patterns vs. EVA vocabulary');
  console.log('');

  // Load vision data with spatial layout
  console.log('Loading folio_vision_analysis...');
  const visionRows = await executeSql(`
    SELECT folio_id, subject_candidates, spatial_layout
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

  // Build folio data objects
  const folios: FolioData[] = [];
  for (const r of visionRows) {
    const folioId = r.folio_id ?? '';
    const evaText = evaByFolio.get(folioId) ?? '';

    let spatialLayout: SpatialLayout | null = null;
    try {
      const parsed = JSON.parse(r.spatial_layout ?? '{}');
      if (parsed.text_regions) spatialLayout = parsed as SpatialLayout;
    } catch { /* ok */ }

    let candidates: Array<{ name: string; confidence: number }> = [];
    try { candidates = JSON.parse(r.subject_candidates ?? '[]'); } catch { /* ok */ }
    const top = candidates[0] ?? { name: 'unknown', confidence: 0 };

    const words = extractEvaWords(evaText);
    const freq = new Map<string, number>();
    for (const w of words) freq.set(w, (freq.get(w) ?? 0) + 1);
    const hapaxes = [...freq.values()].filter((c) => c === 1).length;

    folios.push({
      folioId,
      subjectCandidates: r.subject_candidates ?? '[]',
      spatialLayout,
      topName: top.name ?? 'unknown',
      topConfidence: top.confidence ?? 0,
      family: classifyPlantFamily(r.subject_candidates ?? '[]'),
      wordCount: words.length,
      typeCount: freq.size,
      avgWordLen: words.length > 0 ? words.reduce((s, w) => s + w.length, 0) / words.length : 0,
      hapaxRate: freq.size > 0 ? hapaxes / freq.size : 0,
    });
  }

  // Sort by manuscript position
  folios.sort((a, b) => folioSortKey(a.folioId) - folioSortKey(b.folioId));

  console.log(`  ${folios.length} folios processed`);

  // ---------------------------------------------------------------------------
  // ANALYSIS 1: "Uncertain" anomaly — manuscript position clustering
  // ---------------------------------------------------------------------------
  console.log('\n=== ANALYSIS 1: "Uncertain" folios — manuscript position ===\n');

  const uncertain = folios.filter((f) => f.family === 'uncertain');
  const herbalProper = folios.filter((f) => f.family !== 'uncertain' && f.family !== 'unknown');

  console.log(`  Total uncertain folios: ${uncertain.length}`);
  console.log(`  Uncertain folios in manuscript order:`);
  console.log('');

  // Show runs of consecutive uncertain folios
  let runStart: FolioData | null = null;
  let runLen = 0;
  const allSortedFolios = folios.map((f) => ({ ...f, sortKey: folioSortKey(f.folioId) }));
  let lastKey = -99;

  for (const f of allSortedFolios) {
    const key = folioSortKey(f.folioId);
    if (f.family === 'uncertain') {
      if (runStart === null || key - lastKey > 1.1) {
        if (runStart !== null && runLen > 0) {
          // already printed
        }
        runStart = f;
        runLen = 1;
      } else {
        runLen++;
      }
      console.log(`    ${f.folioId.padEnd(8)} conf=${f.topConfidence.toFixed(2)} name="${f.topName.slice(0, 50)}"`);
      lastKey = key;
    } else {
      lastKey = key;
    }
  }

  // Check for clustering: compute gap statistics
  const uncertainKeys = uncertain.map((f) => folioSortKey(f.folioId)).sort((a, b) => a - b);
  const gaps: number[] = [];
  for (let i = 1; i < uncertainKeys.length; i++) gaps.push(uncertainKeys[i] - uncertainKeys[i - 1]);
  const meanGap = gaps.length > 0 ? gaps.reduce((s, g) => s + g, 0) / gaps.length : 0;
  const maxGap = gaps.length > 0 ? Math.max(...gaps) : 0;
  const medianGap = gaps.length > 0 ? gaps.sort((a, b) => a - b)[Math.floor(gaps.length / 2)] : 0;

  // Expected gap if uniformly distributed across all folio positions
  const allKeys = folios.map((f) => folioSortKey(f.folioId)).sort((a, b) => a - b);
  const expectedGap = (allKeys[allKeys.length - 1] - allKeys[0]) / (uncertain.length + 1);

  console.log('');
  console.log(`  Gap statistics between uncertain folios:`);
  console.log(`    mean gap:     ${meanGap.toFixed(2)}  (expected if random: ${expectedGap.toFixed(2)})`);
  console.log(`    median gap:   ${medianGap.toFixed(2)}`);
  console.log(`    max gap:      ${maxGap.toFixed(2)}`);
  console.log('');
  if (meanGap < expectedGap * 0.5) {
    console.log('  CLUSTERED: uncertain folios are significantly more clustered than random.');
    console.log('  Their EVA vocabulary overlap is likely a quire/positional artifact.');
  } else if (meanGap < expectedGap * 0.8) {
    console.log('  SOMEWHAT CLUSTERED: modest positional clustering of uncertain folios.');
    console.log('  Partial positional artifact is likely.');
  } else {
    console.log('  NOT CLUSTERED: uncertain folios are distributed across the manuscript.');
    console.log('  Their EVA vocabulary overlap reflects shared content structure, not position.');
  }

  // ---------------------------------------------------------------------------
  // ANALYSIS 2: Layout features by family
  // ---------------------------------------------------------------------------
  console.log('\n=== ANALYSIS 2: Spatial layout features by plant family ===\n');

  // Extract layout features per folio
  interface LayoutFeatures {
    numRegions: number;
    hasLabel: boolean;
    hasDescription: boolean;
    positions: string[];
    totalLines: number;
  }

  function extractLayoutFeatures(sl: SpatialLayout | null): LayoutFeatures {
    if (!sl || !sl.text_regions || sl.text_regions.length === 0) {
      return { numRegions: 0, hasLabel: false, hasDescription: false, positions: [], totalLines: 0 };
    }
    return {
      numRegions: sl.text_regions.length,
      hasLabel: sl.text_regions.some((r) => r.role === 'label'),
      hasDescription: sl.text_regions.some((r) => r.role === 'description'),
      positions: sl.text_regions.map((r) => r.position),
      totalLines: sl.text_regions.reduce((s, r) => s + (r.estimated_lines ?? 0), 0),
    };
  }

  // Aggregate by family
  const familyStats = new Map<string, {
    folios: FolioData[];
    features: LayoutFeatures[];
  }>();

  for (const f of folios) {
    if (!familyStats.has(f.family)) familyStats.set(f.family, { folios: [], features: [] });
    const fam = familyStats.get(f.family)!;
    fam.folios.push(f);
    fam.features.push(extractLayoutFeatures(f.spatialLayout));
  }

  // Print per-family table
  const mean = (arr: number[]) => arr.length > 0 ? arr.reduce((s, x) => s + x, 0) / arr.length : 0;
  const pct = (arr: boolean[]) => arr.length > 0 ? (arr.filter(Boolean).length / arr.length * 100) : 0;

  console.log(
    '  ' + 'family'.padEnd(20) +
    'n'.padStart(4) +
    'regions'.padStart(9) +
    'lines'.padStart(7) +
    '%label'.padStart(8) +
    '%desc'.padStart(7) +
    'words'.padStart(7) +
    'types'.padStart(7) +
    'avgLen'.padStart(8)
  );
  console.log('  ' + '-'.repeat(72));

  const sortedFamilies = [...familyStats.entries()].sort((a, b) => b[1].folios.length - a[1].folios.length);
  for (const [fam, { folios: fps, features }] of sortedFamilies) {
    if (fps.length < 3) continue;
    const numR = mean(features.map((f) => f.numRegions));
    const numL = mean(features.map((f) => f.totalLines));
    const pLabel = pct(features.map((f) => f.hasLabel));
    const pDesc = pct(features.map((f) => f.hasDescription));
    const avgWords = mean(fps.map((f) => f.wordCount));
    const avgTypes = mean(fps.map((f) => f.typeCount));
    const avgLen = mean(fps.map((f) => f.avgWordLen));

    console.log(
      '  ' + fam.padEnd(20) +
      fps.length.toString().padStart(4) +
      numR.toFixed(1).padStart(9) +
      numL.toFixed(1).padStart(7) +
      pLabel.toFixed(0).padStart(8) +
      pDesc.toFixed(0).padStart(7) +
      avgWords.toFixed(0).padStart(7) +
      avgTypes.toFixed(0).padStart(7) +
      avgLen.toFixed(2).padStart(8)
    );
  }

  // ---------------------------------------------------------------------------
  // ANALYSIS 3: Section fingerprinting — non-herbal vs herbal proper
  // ---------------------------------------------------------------------------
  console.log('\n=== ANALYSIS 3: Non-herbal vs. herbal section fingerprinting ===\n');

  // Non-herbal = zodiac/astronomical/bathing identifications despite being in herbal EVA corpus
  const nonHerbalKeywords = /zodiac|astrolog|astronomical|cosmolog|bathing|nymph|balneolog|pisces|aries|taurus|libra|sagittarius|scorpio|capricorn/i;

  const nonHerbal = folios.filter((f) => nonHerbalKeywords.test(f.topName));
  const herbalIdentified = folios.filter((f) => !nonHerbalKeywords.test(f.topName) && f.family !== 'uncertain' && f.family !== 'unknown');

  console.log(`  Non-herbal folios misclassified into herbal EVA section: ${nonHerbal.length}`);
  console.log(`  Properly identified herbal folios: ${herbalIdentified.length}`);
  console.log('');

  // Compare EVA stats
  const nhWords = mean(nonHerbal.map((f) => f.wordCount));
  const nhTypes = mean(nonHerbal.map((f) => f.typeCount));
  const nhLen = mean(nonHerbal.map((f) => f.avgWordLen));
  const nhHapax = mean(nonHerbal.map((f) => f.hapaxRate));

  const hWords = mean(herbalIdentified.map((f) => f.wordCount));
  const hTypes = mean(herbalIdentified.map((f) => f.typeCount));
  const hLen = mean(herbalIdentified.map((f) => f.avgWordLen));
  const hHapax = mean(herbalIdentified.map((f) => f.hapaxRate));

  console.log('              ' + 'herbal-proper'.padStart(16) + 'non-herbal'.padStart(12));
  console.log('  ' + '-'.repeat(40));
  console.log(`  word count  ${hWords.toFixed(1).padStart(16)} ${nhWords.toFixed(1).padStart(12)}`);
  console.log(`  type count  ${hTypes.toFixed(1).padStart(16)} ${nhTypes.toFixed(1).padStart(12)}`);
  console.log(`  avg word len${hLen.toFixed(2).padStart(16)} ${nhLen.toFixed(2).padStart(12)}`);
  console.log(`  hapax rate  ${hHapax.toFixed(3).padStart(16)} ${nhHapax.toFixed(3).padStart(12)}`);
  console.log('');

  if (Math.abs(hLen - nhLen) > 0.15) {
    console.log(`  FINDING: avg word length differs by ${Math.abs(hLen - nhLen).toFixed(2)} chars.`);
    console.log(`  ${hLen > nhLen ? 'Herbal' : 'Non-herbal'} folios have longer average EVA words.`);
  }
  if (Math.abs(hHapax - nhHapax) > 0.05) {
    console.log(`  FINDING: hapax rate differs by ${Math.abs(hHapax - nhHapax).toFixed(3)}.`);
    console.log(`  ${hHapax > nhHapax ? 'Herbal' : 'Non-herbal'} folios have more unique one-off EVA words.`);
  }
  if (Math.abs(hWords - nhWords) > 20) {
    console.log(`  FINDING: word count differs by ${Math.abs(hWords - nhWords).toFixed(0)} words per folio.`);
    console.log(`  ${hWords > nhWords ? 'Herbal' : 'Non-herbal'} folios have more EVA text.`);
  }

  // ---------------------------------------------------------------------------
  // ANALYSIS 4: Position vocabulary — do text regions with label vs. description roles differ?
  // ---------------------------------------------------------------------------
  console.log('\n=== ANALYSIS 4: Text region positions across the manuscript ===\n');

  const positionCounts = new Map<string, number>();
  const roleCounts = new Map<string, number>();
  let totalRegions = 0;
  let foliosWithLayout = 0;

  for (const f of folios) {
    if (!f.spatialLayout) continue;
    foliosWithLayout++;
    for (const r of f.spatialLayout.text_regions) {
      positionCounts.set(r.position, (positionCounts.get(r.position) ?? 0) + 1);
      roleCounts.set(r.role, (roleCounts.get(r.role) ?? 0) + 1);
      totalRegions++;
    }
  }

  console.log(`  ${foliosWithLayout} of ${folios.length} folios have spatial layout data`);
  console.log(`  Total text regions: ${totalRegions} (avg ${(totalRegions / foliosWithLayout).toFixed(1)} per folio)`);
  console.log('');
  console.log('  Text region positions:');
  for (const [pos, cnt] of [...positionCounts.entries()].sort((a, b) => b[1] - a[1])) {
    const bar = '█'.repeat(Math.round(cnt / totalRegions * 40));
    console.log(`    ${pos.padEnd(16)} ${cnt.toString().padStart(3)}  ${bar}`);
  }
  console.log('');
  console.log('  Text region roles:');
  for (const [role, cnt] of [...roleCounts.entries()].sort((a, b) => b[1] - a[1])) {
    const bar = '█'.repeat(Math.round(cnt / totalRegions * 40));
    console.log(`    ${role.padEnd(16)} ${cnt.toString().padStart(3)}  ${bar}`);
  }

  // Folio EVA word count vs. number of layout regions (correlation)
  const foliosWithBoth = folios.filter((f) => f.spatialLayout && f.wordCount > 0);
  if (foliosWithBoth.length > 10) {
    const xVals = foliosWithBoth.map((f) => f.spatialLayout!.text_regions.length);
    const yVals = foliosWithBoth.map((f) => f.wordCount);
    const meanX = mean(xVals);
    const meanY = mean(yVals);
    let num = 0, denX = 0, denY = 0;
    for (let i = 0; i < xVals.length; i++) {
      num += (xVals[i] - meanX) * (yVals[i] - meanY);
      denX += (xVals[i] - meanX) ** 2;
      denY += (yVals[i] - meanY) ** 2;
    }
    const r = denX === 0 || denY === 0 ? 0 : num / Math.sqrt(denX * denY);
    console.log('');
    console.log(`  Correlation: num_text_regions vs. EVA word count: r=${r.toFixed(3)}`);
    if (Math.abs(r) > 0.3) {
      console.log(`  FINDING: ${r > 0 ? 'More' : 'Fewer'} text regions → ${r > 0 ? 'more' : 'fewer'} EVA words (|r|=${Math.abs(r).toFixed(2)}).`);
    } else {
      console.log('  Text region count and EVA word count are not strongly correlated.');
    }
  }

  // ---------------------------------------------------------------------------
  // VERDICT
  // ---------------------------------------------------------------------------
  console.log('\n=== VERDICT ===\n');
  console.log('  1. "Uncertain" clustering: ' + (meanGap < expectedGap * 0.7 ? 'YES — positional artifact likely.' : 'AMBIGUOUS — spread across manuscript.'));
  console.log('  2. Layout differs by family: check per-family table above for region count / line count differences.');
  console.log('  3. Non-herbal section: EVA word characteristics reveal' + (Math.abs(hLen - nhLen) > 0.1 ? ' distinguishable' : ' indistinguishable') + ' signature vs. herbal proper.');
  console.log('  4. Text region positions: see distribution above.');
}

main().catch((err) => {
  console.error('[spatial-layout-analysis] fatal:', err);
  process.exit(1);
});
