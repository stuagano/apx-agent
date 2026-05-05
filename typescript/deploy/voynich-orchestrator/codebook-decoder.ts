/**
 * Vocabulary-guided codebook decoder.
 *
 * Seeds a partial codebook from statistically confirmed anchor words.
 * Each anchor passes two independent gates:
 *   1. NPMI ≥ 0.40 against a discriminating expected term (≤40 folios)
 *   2. Full-distribution family hit rate ≥ 70%
 *      (home_family_hits / n_total across ALL herbal folios, no confidence filter on denominator)
 *
 * Confirmed anchors:
 *   oldaiin → somnus    (solanaceae — sleep/soporific)
 *     Gate: 87.5% solanaceae hit rate (7/8 folios), NPMI=0.707 vs beveraggio
 *
 * For each high-confidence herbal folio, applies the codebook and reports:
 *   - Anchor hits: which anchors appear and how often
 *   - Family alignment: are the right family's anchors lighting up?
 *   - Decoded text sample showing anchors in context
 *   - Critic score on the decoded fragment
 *
 * No SA — pure lookup. Anchors are constraints, not starting points.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   export CRITIC_AGENT_URL=https://...   # optional; skip critic if unset
 *   npx tsx codebook-decoder.ts
 *
 * Env:
 *   MIN_CONF=0.40      minimum vision confidence to include a folio
 *   SHOW_TEXT=1        print decoded text for each folio (verbose)
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const MIN_CONF  = parseFloat(process.env.MIN_CONF ?? '0.40');
const SHOW_TEXT = process.env.SHOW_TEXT === '1';

// ---------------------------------------------------------------------------
// Codebook — EVA word → Latin term + metadata
// ---------------------------------------------------------------------------

interface AnchorEntry {
  latin:  string;
  gloss:  string;
  family: 'solanaceae' | 'thistle';
}

const CODEBOOK: Record<string, AnchorEntry> = {
  oldaiin: { latin: 'somnus', gloss: 'sleep/soporific', family: 'solanaceae' },
};

const ANCHOR_WORDS = Object.keys(CODEBOOK);

// ---------------------------------------------------------------------------
// SQL helper
// ---------------------------------------------------------------------------

async function executeSql(statement: string): Promise<Array<Record<string, string | null>>> {
  const host       = resolveHost();
  const token      = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');

  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '30s' }),
  });
  if (!res.ok) throw new Error(`SQL ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as {
    result?:   { data_array?: (string | null)[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> } };
    status?:   { state?: string; error?: { message?: string } };
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
// Critic helper (graceful no-op if CRITIC_AGENT_URL unset)
// ---------------------------------------------------------------------------

async function scoreLatin(text: string): Promise<number | null> {
  const criticUrl = process.env.CRITIC_AGENT_URL;
  if (!criticUrl || text.trim().length < 10) return null;

  try {
    const token = await resolveToken().catch(() => '');
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (token) headers.Authorization = `Bearer ${token}`;

    const res = await fetch(
      `${criticUrl.replace(/\/$/, '')}/api/agent/tools/score_latin_likelihood`,
      { method: 'POST', headers, body: JSON.stringify({ decoded_text: text, source_language: 'latin' }) },
    );
    if (!res.ok) return null;
    const data = (await res.json()) as { likelihood?: number };
    return data.likelihood ?? null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// EVA tokeniser
// ---------------------------------------------------------------------------

function tokenise(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, ' ')
    .split(/[\s.]+/)
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w));
}

// ---------------------------------------------------------------------------
// Family classifier (mirrors pmi-analysis.ts)
// ---------------------------------------------------------------------------

function classifyFamily(subjectCandidates: string | null): 'solanaceae' | 'thistle' | 'other' {
  let cands: Array<{ name?: string; latin?: string; confidence?: number }> = [];
  try { cands = JSON.parse(subjectCandidates ?? '[]'); } catch { return 'other'; }
  const top = cands[0];
  if (!top || (top.confidence ?? 0) < 0.35) return 'other';
  const key = ((top.latin ?? '') + ' ' + (top.name ?? '')).toLowerCase();
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus/.test(key)) return 'solanaceae';
  if (/cirsium|carduus|carlina|centaurea|silybum|onopordum|echinops/.test(key)) return 'thistle';
  return 'other';
}

// ---------------------------------------------------------------------------
// Decoded text builder
//   Unknown words shown as "_" (length-proportional), anchors as «LATIN»
// ---------------------------------------------------------------------------

function buildDecodedLine(tokens: string[]): string {
  return tokens.map((t) => {
    const entry = CODEBOOK[t];
    return entry ? `«${entry.latin.toUpperCase()}»` : '_'.repeat(Math.min(t.length, 6));
  }).join(' ');
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

interface FolioResult {
  folioId:     string;
  plantName:   string;
  conf:        number;
  family:      'solanaceae' | 'thistle' | 'other';
  totalTokens: number;
  hits:        Record<string, number>;   // anchor word → count of appearances
  decodedWords: string[];                // only the latin substitutions
  criticScore: number | null;
}

async function main(): Promise<void> {
  console.log('codebook-decoder — vocabulary-guided anchor decoding');
  console.log(`MIN_CONF=${MIN_CONF}  SHOW_TEXT=${SHOW_TEXT}`);
  console.log('');
  console.log('Codebook anchors:');
  for (const [eva, entry] of Object.entries(CODEBOOK)) {
    console.log(`  ${eva.padEnd(10)} → ${entry.latin.padEnd(12)} (${entry.gloss}, family=${entry.family})`);
  }
  console.log('');

  // Load herbal folios meeting confidence threshold
  console.log(`Loading herbal folios (conf ≥ ${MIN_CONF})...`);
  const visionRows = await executeSql(`
    SELECT folio_id, subject_candidates,
           get_json_object(subject_candidates, '$[0].name') AS plant_name,
           CAST(get_json_object(subject_candidates, '$[0].confidence') AS DOUBLE) AS conf
    FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    WHERE section = 'herbal'
      AND CAST(get_json_object(subject_candidates, '$[0].confidence') AS DOUBLE) >= ${MIN_CONF}
    ORDER BY folio_id
  `);

  const evaRows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
  `);
  const evaByFolio = new Map<string, string>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaByFolio.set(r.folio_id, r.eva_text);
  }

  console.log(`  ${visionRows.length} vision rows above threshold`);
  console.log('');

  // Process each folio
  const results: FolioResult[] = [];

  for (const r of visionRows) {
    const folioId = r.folio_id ?? '';
    const evaText = evaByFolio.get(folioId);
    if (!evaText) continue;

    const conf       = parseFloat(r.conf ?? '0');
    const family     = classifyFamily(r.subject_candidates);
    const tokens     = tokenise(evaText);
    const totalTokens = tokens.length;

    // Count anchor hits
    const hits: Record<string, number> = {};
    const decodedWords: string[] = [];
    for (const t of tokens) {
      const entry = CODEBOOK[t];
      if (entry) {
        hits[t] = (hits[t] ?? 0) + 1;
        decodedWords.push(entry.latin);
      }
    }

    const totalHits = Object.values(hits).reduce((s, c) => s + c, 0);

    let criticScore: number | null = null;
    if (decodedWords.length >= 2) {
      criticScore = await scoreLatin(decodedWords.join(' '));
    }

    results.push({ folioId, plantName: r.plant_name ?? '', conf, family, totalTokens, hits, decodedWords, criticScore });

    if (SHOW_TEXT && totalHits > 0) {
      const decoded = buildDecodedLine(tokens);
      console.log(`  [${folioId}] ${r.plant_name} (${family}, conf=${conf.toFixed(2)})`);
      console.log(`    ${decoded}`);
      console.log('');
    }
  }

  // ---------------------------------------------------------------------------
  // Summary tables
  // ---------------------------------------------------------------------------

  console.log('=== Anchor hit counts per folio ===');
  console.log('');
  console.log(
    'folio'.padEnd(10) +
    'family'.padEnd(12) +
    'conf'.padStart(5) + ' ' +
    'tok'.padStart(4) + '  ' +
    ANCHOR_WORDS.map((w) => w.padStart(8)).join('') + '  total'
  );
  console.log('─'.repeat(10 + 12 + 6 + 6 + ANCHOR_WORDS.length * 8 + 7));

  const foliosWithHits = results.filter((r) => Object.keys(r.hits).length > 0);

  for (const r of foliosWithHits) {
    const total = Object.values(r.hits).reduce((s, c) => s + c, 0);
    console.log(
      r.folioId.padEnd(10) +
      r.family.padEnd(12) +
      r.conf.toFixed(2).padStart(5) + ' ' +
      r.totalTokens.toString().padStart(4) + '  ' +
      ANCHOR_WORDS.map((w) => (r.hits[w] ?? 0).toString().padStart(8)).join('') +
      '  ' + total.toString()
    );
  }

  console.log('');
  console.log(`  Folios with ≥1 anchor hit: ${foliosWithHits.length} / ${results.length}`);

  // ---------------------------------------------------------------------------
  // Family alignment check
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Family alignment — anchor hits by folio family ===');
  console.log('');
  console.log('  (Correctly aligned = anchor family matches folio family)');
  console.log('');

  const alignStats: Record<'solanaceae' | 'thistle' | 'other', Record<string, number>> = {
    solanaceae: {}, thistle: {}, other: {},
  };

  for (const r of results) {
    for (const [word, count] of Object.entries(r.hits)) {
      alignStats[r.family][word] = (alignStats[r.family][word] ?? 0) + count;
    }
  }

  for (const family of ['solanaceae', 'thistle', 'other'] as const) {
    const totals = alignStats[family];
    if (Object.keys(totals).length === 0) continue;
    console.log(`  ${family}:`);
    for (const word of ANCHOR_WORDS) {
      const count = totals[word] ?? 0;
      if (count === 0) continue;
      const entry = CODEBOOK[word];
      const aligned = entry.family === family ? '✓' : '✗';
      console.log(`    ${aligned} ${word.padEnd(10)} → ${entry.latin.padEnd(12)}: ${count} hits`);
    }
    console.log('');
  }

  // ---------------------------------------------------------------------------
  // Coverage and critic scores
  // ---------------------------------------------------------------------------

  const totalTokensAll  = results.reduce((s, r) => s + r.totalTokens, 0);
  const totalHitsAll    = results.reduce((s, r) => s + Object.values(r.hits).reduce((a, b) => a + b, 0), 0);
  const coverage        = totalTokensAll > 0 ? (totalHitsAll / totalTokensAll * 100) : 0;

  console.log('=== Coverage ===');
  console.log('');
  console.log(`  Total tokens (all folios): ${totalTokensAll}`);
  console.log(`  Total anchor hits:         ${totalHitsAll}`);
  console.log(`  Overall coverage:          ${coverage.toFixed(2)}%`);
  console.log('');

  // Per anchor
  const perAnchorHits: Record<string, number> = {};
  for (const r of results) {
    for (const [w, c] of Object.entries(r.hits)) {
      perAnchorHits[w] = (perAnchorHits[w] ?? 0) + c;
    }
  }
  console.log('  Per-anchor hit counts:');
  for (const word of ANCHOR_WORDS) {
    const entry = CODEBOOK[word];
    console.log(`    ${word.padEnd(10)} → ${entry.latin.padEnd(12)}: ${(perAnchorHits[word] ?? 0).toString().padStart(4)} hits  [family=${entry.family}]`);
  }

  // ---------------------------------------------------------------------------
  // Critic scores — decode solanaceae and thistle fragments separately
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Critic scores on decoded fragments ===');
  console.log('');

  const solanacea_words = results
    .filter((r) => r.family === 'solanaceae')
    .flatMap((r) => r.decodedWords);
  const thistle_words = results
    .filter((r) => r.family === 'thistle')
    .flatMap((r) => r.decodedWords);
  const wrong_family_words = results
    .filter((r) => r.family === 'other')
    .flatMap((r) => r.decodedWords);

  const solanaceaFragment = solanacea_words.join(' ');
  const thistleFragment   = thistle_words.join(' ');
  const wrongFragment     = wrong_family_words.join(' ');

  console.log(`  Solanaceae decoded fragment (${solanacea_words.length} words): ${solanaceaFragment || '(none)'}`);
  console.log(`  Thistle decoded fragment (${thistle_words.length} words):   ${thistleFragment || '(none)'}`);
  console.log(`  Other-family fragment (${wrong_family_words.length} words):    ${wrongFragment || '(none)'}`);
  console.log('');

  if (process.env.CRITIC_AGENT_URL) {
    const [sScore, tScore, oScore] = await Promise.all([
      scoreLatin(solanaceaFragment),
      scoreLatin(thistleFragment),
      scoreLatin(wrongFragment),
    ]);
    console.log(`  Critic score — solanaceae: ${sScore !== null ? sScore.toFixed(3) : 'n/a'}`);
    console.log(`  Critic score — thistle:    ${tScore !== null ? tScore.toFixed(3) : 'n/a'}`);
    console.log(`  Critic score — other:      ${oScore !== null ? oScore.toFixed(3) : 'n/a'}`);
    console.log('');
    console.log('  Interpretation: solanaceae and thistle scores should be higher than other-family');
    console.log('  (anchors should light up preferentially on their home family).');
  } else {
    console.log('  (CRITIC_AGENT_URL not set — skipping critic evaluation)');
  }

  // ---------------------------------------------------------------------------
  // Folios with most anchor hits — top 10
  // ---------------------------------------------------------------------------

  console.log('');
  console.log('=== Top 10 folios by anchor hits ===');
  console.log('');

  const ranked = [...results]
    .map((r) => ({ ...r, totalHits: Object.values(r.hits).reduce((s, c) => s + c, 0) }))
    .filter((r) => r.totalHits > 0)
    .sort((a, b) => b.totalHits - a.totalHits)
    .slice(0, 10);

  for (const r of ranked) {
    const hitStr = ANCHOR_WORDS
      .filter((w) => r.hits[w])
      .map((w) => `${w}×${r.hits[w]}`)
      .join(', ');
    console.log(`  ${r.folioId.padEnd(10)} ${r.family.padEnd(12)} conf=${r.conf.toFixed(2)}  ${r.plantName.slice(0, 30).padEnd(30)}  ${hitStr}`);
  }

  // ---------------------------------------------------------------------------
  // Show decoded text for top folios if SHOW_TEXT not set
  // ---------------------------------------------------------------------------

  if (!SHOW_TEXT) {
    console.log('');
    console.log('=== Decoded context — top 5 folios ===');
    console.log('');
    for (const r of ranked.slice(0, 5)) {
      const evaText  = evaByFolio.get(r.folioId) ?? '';
      const tokens   = tokenise(evaText);
      // Show only lines/segments that contain an anchor hit
      const segments: string[] = [];
      let buf: string[] = [];
      let hasHit = false;
      for (const t of tokens) {
        buf.push(t);
        if (CODEBOOK[t]) hasHit = true;
        if (buf.length >= 8) {
          if (hasHit) segments.push(buildDecodedLine(buf));
          buf = [];
          hasHit = false;
        }
      }
      if (hasHit && buf.length) segments.push(buildDecodedLine(buf));

      console.log(`  [${r.folioId}] ${r.plantName.slice(0, 40)} (${r.family}, conf=${r.conf.toFixed(2)})`);
      for (const seg of segments.slice(0, 4)) {
        console.log(`    ${seg}`);
      }
      console.log('');
    }
  }
}

main().catch((err) => {
  console.error('[codebook-decoder] fatal:', err);
  process.exit(1);
});
