/**
 * Solanaceae-signal test: cross-section distribution + NPMI for top solanaceae words.
 *
 * Two questions:
 *
 *   1. CROSS-SECTION: Do the solanaceae-specific EVA words (identified by
 *      prefix-suffix-axis.ts) appear uniformly across the manuscript, or are
 *      they herbal-section specific? Scribal formula words (high-frequency
 *      grammatical particles) would appear in zodiac, balneological, and other
 *      sections at similar rates. Content words specific to botanical subject
 *      matter would be herbal-concentrated — and ideally solanaceae-concentrated
 *      within the herbal section.
 *
 *   2. NPMI vs. EXPECTED TERMS: For each solanaceae-specific word, what Latin
 *      expected terms co-occur on the same folios? High NPMI with pharmacological
 *      terms (narcotics, soporifica, sedativa) would strengthen the content signal.
 *      'oldaiin' already has a documented anchor → somnus (NPMI=0.707) from the
 *      anchor-loop; this test checks the broader solanaceae word set.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx solanaceae-signal-test.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

// Solanaceae-specific words from prefix-suffix-axis.ts (LOR ≥ 2.3, count-in ≥ 4)
const TARGET_WORDS = [
  'kain', 'oldaiin', 'chedaiin', 'pchedy', 'lchedy', 'chdaiin',
  'sheety', 'sheckhy', 'cheom', 'qokeo', 'lshey', 'qopchedy',
  'otaldy', 'qoeey', 'okeo',
];

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
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '50s' }),
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
// Helpers
// ---------------------------------------------------------------------------

function extractEvaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

function parseExpectedTerms(raw: string | null): string[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as { latin?: unknown; italian?: unknown };
    const toArr = (v: unknown): string[] =>
      Array.isArray(v) ? v.map((s) => String(s).toLowerCase().trim()).filter(Boolean) : [];
    return [...new Set([...toArr(parsed.latin), ...toArr(parsed.italian)])];
  } catch { return []; }
}

function classifySection(section: string | null): string {
  const s = (section ?? '').toLowerCase();
  if (s === 'herbal') return 'herbal';
  if (s.includes('zodiac') || s.includes('astro')) return 'zodiac';
  if (s.includes('balneo') || s.includes('bath')) return 'balneo';
  if (s.includes('cosmo') || s.includes('pharma')) return 'other';
  return s || 'unknown';
}

function npmi(pxy: number, px: number, py: number): number {
  if (pxy === 0 || px === 0 || py === 0) return -1;
  return Math.log(pxy / (px * py)) / -Math.log(pxy);
}

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
  if (/plantago|plantain/.test(latin + name)) return 'plantago';
  if (/foeniculum|apium|petroselinum|coriandrum|anethum|daucus/.test(latin + name)) return 'apiaceae';
  if (/verbena/.test(latin + name)) return 'verbena';
  if (/artemisia|absinthium|wormwood/.test(latin + name)) return 'artemisia';
  if (/iris|lily|lilium|crocus/.test(latin + name)) return 'lily-family';
  return 'other-botanical';
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('solanaceae-signal-test — cross-section distribution + NPMI for top solanaceae words');
  console.log(`Target words (${TARGET_WORDS.length}): ${TARGET_WORDS.join(', ')}`);
  console.log('');

  // Load EVA corpus — ALL sections
  console.log('Loading EVA corpus (all sections)...');
  const evaRows = await executeSql(`
    SELECT folio_id, section, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    ORDER BY section, folio_id
  `);

  // Load herbal vision data for family classification
  console.log('Loading folio_vision_analysis (herbal)...');
  const visionRows = await executeSql(`
    SELECT folio_id, subject_candidates, expected_terms
    FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    WHERE section = 'herbal'
    ORDER BY folio_id
  `);

  const familyMap = new Map<string, string>();
  const expectedTermsMap = new Map<string, string[]>();
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    familyMap.set(r.folio_id, classifyPlantFamily(r.subject_candidates ?? '[]'));
    expectedTermsMap.set(r.folio_id, parseExpectedTerms(r.expected_terms));
  }

  // Build folio index
  interface FolioRecord {
    folioId: string;
    section: string;
    sectionGroup: string;
    family: string;
    words: Set<string>;
  }

  const folios: FolioRecord[] = [];
  for (const r of evaRows) {
    if (!r.folio_id || !r.eva_text) continue;
    const words = new Set(extractEvaWords(r.eva_text));
    if (words.size < 5) continue;
    const section = r.section ?? 'unknown';
    const sectionGroup = classifySection(section);
    const family = (sectionGroup === 'herbal') ? (familyMap.get(r.folio_id) ?? 'uncertain') : sectionGroup;
    folios.push({ folioId: r.folio_id, section, sectionGroup, family, words });
  }

  // Count folios per section group
  const sectionCounts = new Map<string, number>();
  for (const f of folios) sectionCounts.set(f.sectionGroup, (sectionCounts.get(f.sectionGroup) ?? 0) + 1);
  const totalFolios = folios.length;

  console.log(`  ${totalFolios} folios with EVA text across ${sectionCounts.size} section groups`);
  for (const [s, n] of [...sectionCounts.entries()].sort((a, b) => b[1] - a[1])) {
    console.log(`    ${s.padEnd(16)} n=${n}`);
  }
  console.log('');

  // Within herbal: family counts
  const herbalFolios = folios.filter((f) => f.sectionGroup === 'herbal');
  const familyCounts = new Map<string, number>();
  for (const f of herbalFolios) familyCounts.set(f.family, (familyCounts.get(f.family) ?? 0) + 1);
  const solanFolios = herbalFolios.filter((f) => f.family === 'solanaceae');
  const otherHerbalFolios = herbalFolios.filter((f) => f.family !== 'solanaceae');

  // ---------------------------------------------------------------------------
  // PART 1: Cross-section prevalence per target word
  // ---------------------------------------------------------------------------
  console.log('=== PART 1: CROSS-SECTION PREVALENCE ===');
  console.log('(prevalence = fraction of section folios where word appears ≥1 time)');
  console.log('');

  // Collect section groups present (sorted by importance)
  const sectionOrder = ['herbal', 'zodiac', 'balneo', 'other', 'unknown'];
  const sectionsPresent = sectionOrder.filter((s) => sectionCounts.has(s));

  // Header
  const hdr = 'Word'.padEnd(14) +
    'solan%'.padEnd(9) + 'oth-herb%'.padEnd(11) +
    sectionsPresent.filter((s) => s !== 'herbal').map((s) => `${s}%`.padEnd(9)).join('') +
    'specificity';
  console.log(hdr);
  console.log('-'.repeat(hdr.length + 5));

  for (const word of TARGET_WORDS) {
    const solanPrev = solanFolios.length > 0
      ? solanFolios.filter((f) => f.words.has(word)).length / solanFolios.length
      : 0;
    const otherHerbalPrev = otherHerbalFolios.length > 0
      ? otherHerbalFolios.filter((f) => f.words.has(word)).length / otherHerbalFolios.length
      : 0;

    const nonHerbalPrevs: Record<string, number> = {};
    for (const sg of sectionsPresent.filter((s) => s !== 'herbal')) {
      const sgFolios = folios.filter((f) => f.sectionGroup === sg);
      nonHerbalPrevs[sg] = sgFolios.length > 0
        ? sgFolios.filter((f) => f.words.has(word)).length / sgFolios.length
        : 0;
    }

    // Specificity: solanaceae prevalence / (all-manuscript prevalence)
    const allPrev = folios.filter((f) => f.words.has(word)).length / totalFolios;
    const specificity = allPrev > 0 ? solanPrev / allPrev : 0;

    const nonHerbalAvg = Object.values(nonHerbalPrevs).length > 0
      ? Object.values(nonHerbalPrevs).reduce((s, v) => s + v, 0) / Object.values(nonHerbalPrevs).length
      : 0;
    const herbalFlag = (solanPrev > otherHerbalPrev * 1.5 && nonHerbalAvg < solanPrev * 0.3) ? ' ★' : '';

    console.log(
      word.padEnd(14) +
      `${(solanPrev * 100).toFixed(1)}%`.padEnd(9) +
      `${(otherHerbalPrev * 100).toFixed(1)}%`.padEnd(11) +
      sectionsPresent.filter((s) => s !== 'herbal').map((s) =>
        `${(nonHerbalPrevs[s] * 100).toFixed(1)}%`.padEnd(9)
      ).join('') +
      `${specificity.toFixed(2)}x${herbalFlag}`,
    );
  }
  console.log('');
  console.log('★ = solanaceae prevalence >1.5x other-herbal AND non-herbal avg <30% of solan rate');
  console.log('');

  // ---------------------------------------------------------------------------
  // PART 2: NPMI vs. expected Latin terms
  // ---------------------------------------------------------------------------
  console.log('=== PART 2: NPMI vs. EXPECTED TERMS (herbal folios only) ===');
  console.log('');

  // Build folio-level index of (word, term) co-occurrences
  const wordFolioSet = new Map<string, Set<string>>();   // word → set of folio_ids where it appears
  const termFolioSet = new Map<string, Set<string>>();   // term → set of folio_ids where it's expected

  for (const f of herbalFolios) {
    for (const w of TARGET_WORDS) {
      if (f.words.has(w)) {
        const s = wordFolioSet.get(w) ?? new Set<string>();
        s.add(f.folioId);
        wordFolioSet.set(w, s);
      }
    }
    const terms = expectedTermsMap.get(f.folioId) ?? [];
    for (const t of terms) {
      const s = termFolioSet.get(t) ?? new Set<string>();
      s.add(f.folioId);
      termFolioSet.set(t, s);
    }
  }

  const N = herbalFolios.length;

  for (const word of TARGET_WORDS) {
    const wordFolios = wordFolioSet.get(word) ?? new Set<string>();
    if (wordFolios.size === 0) {
      console.log(`${word}: not found on any herbal folio`);
      continue;
    }

    const px = wordFolios.size / N;

    const termScores: Array<{ term: string; npmi: number; joint: number; termN: number }> = [];
    for (const [term, termFolios] of termFolioSet) {
      if (termFolios.size < 2) continue;
      let joint = 0;
      for (const fid of wordFolios) if (termFolios.has(fid)) joint++;
      if (joint < 2) continue;
      const py = termFolios.size / N;
      const pxy = joint / N;
      const score = npmi(pxy, px, py);
      termScores.push({ term, npmi: score, joint, termN: termFolios.size });
    }

    termScores.sort((a, b) => b.npmi - a.npmi);
    const top5 = termScores.slice(0, 5);

    const familyBreakdown = herbalFolios
      .filter((f) => f.words.has(word))
      .reduce((acc, f) => {
        acc[f.family] = (acc[f.family] ?? 0) + 1;
        return acc;
      }, {} as Record<string, number>);
    const famStr = Object.entries(familyBreakdown)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([f, n]) => `${f}:${n}`)
      .join(', ');

    console.log(`${word}  (${wordFolios.size} folios: ${famStr})`);
    if (top5.length === 0) {
      console.log('  No NPMI associations (insufficient co-occurrences)');
    } else {
      for (const { term, npmi: score, joint, termN } of top5) {
        console.log(`  NPMI=${score.toFixed(3)}  term="${term}"  joint=${joint}  term_n=${termN}`);
      }
    }
    console.log('');
  }

  // ---------------------------------------------------------------------------
  // PART 3: Verdict
  // ---------------------------------------------------------------------------
  console.log('=== VERDICT ===');
  console.log('');

  let herbalSpecificCount = 0;
  let solanSpecificCount = 0;

  for (const word of TARGET_WORDS) {
    const solanPrev = solanFolios.length > 0
      ? solanFolios.filter((f) => f.words.has(word)).length / solanFolios.length
      : 0;
    const nonHerbal = folios
      .filter((f) => f.sectionGroup !== 'herbal')
      .filter((f) => f.words.has(word));
    const nonHerbalPrev = nonHerbal.length / Math.max(1, folios.filter((f) => f.sectionGroup !== 'herbal').length);

    if (nonHerbalPrev < solanPrev * 0.3) herbalSpecificCount++;
    if (solanPrev > otherHerbalFolios.filter((f) => f.words.has(word)).length / Math.max(1, otherHerbalFolios.length) * 1.5) {
      solanSpecificCount++;
    }
  }

  console.log(`${herbalSpecificCount}/${TARGET_WORDS.length} target words have <30% non-herbal prevalence relative to solanaceae rate (herbal-specific)`);
  console.log(`${solanSpecificCount}/${TARGET_WORDS.length} target words are >1.5x more prevalent in solanaceae than other herbal families`);
  console.log('');

  if (herbalSpecificCount >= TARGET_WORDS.length * 0.7 && solanSpecificCount >= TARGET_WORDS.length * 0.5) {
    console.log('VERDICT: CONTENT WORDS — top solanaceae EVA words are herbal-specific and solanaceae-concentrated.');
    console.log('         This is strong positive evidence against scribal formula and for botanical content encoding.');
  } else if (herbalSpecificCount >= TARGET_WORDS.length * 0.5) {
    console.log('VERDICT: PARTIAL — words are herbal-specific but solanaceae concentration is weaker than expected.');
  } else {
    console.log('VERDICT: FORMULA WORDS — non-herbal prevalence is too high; these appear to be scribal formula words.');
  }
}

main().catch((err) => {
  console.error('[solanaceae-signal-test] fatal:', err);
  process.exit(1);
});
