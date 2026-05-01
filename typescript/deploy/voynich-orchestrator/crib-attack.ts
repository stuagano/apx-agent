/**
 * Crib attack: zodiac month label cribs.
 *
 * The Voynich zodiac folios (f70v–f73v) contain short EVA text labels next to
 * nymph figures arranged in zodiac wheel layouts. Some of these labels have
 * been plausibly identified as month names:
 *
 *   March (Aries)   → "mor" / "marc" / "martius"
 *   April (Taurus)  → "apr" / "aprel" / "aprilis"
 *   May (Gemini)    → "mai" / "mayo" / "maius"
 *   June (Cancer)   → "iun" / "junho" / "iunius"
 *   July (Leo)      → "iul" / "iulio" / "iulius"
 *   August (Virgo)  → "ago" / "agos" / "augustus"
 *
 * Several EVA nymph labels in those folios appear repeatedly and are short
 * (2-6 tokens), making them natural crib candidates. The EVA labels are
 * typically written next to the nymph figure, not in the marginal Occitan
 * notation.
 *
 * This script:
 *   1. Queries for astronomical/zodiac section folios and their EVA text
 *   2. Extracts short EVA words (2-5 tokens) from those folios
 *   3. Identifies which short words appear ≥ 5 times (recurring label candidates)
 *   4. For each candidate crib pair (EVA-label, Latin-month), builds a partial
 *      substitution map (what letter→letter assignments does this crib imply?)
 *   5. Extends the partial map via SA on the herbal corpus, fixing crib assignments
 *   6. Scores with the existing hill-climbing scorer
 *   7. Reports whether any crib pair produces a plausible decode
 *
 * If the table has no astronomical section:
 *   - Falls back to the well-known zodiac label EVA candidates from published
 *     Voynich transcription research (o'tchoey, otshey, etc.)
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx crib-attack.ts
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
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => {
    const obj: Record<string, string | null> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

// ---------------------------------------------------------------------------
// EVA tokenizer (multi-glyph)
// ---------------------------------------------------------------------------

const EVA_MULTI_GLYPHS = ['ch', 'sh', 'th', 'ct', 'ck', 'qo', 'ok', 'ol', 'ai', 'ee', 'dy', 'ey', 'or', 'ar'];

function tokenizeEva(evaText: string): string[] {
  const text = evaText.replace(/\./g, ' ');
  const tokens: string[] = [];
  let i = 0;
  while (i < text.length) {
    if (text[i] === ' ') { i++; continue; }
    let matched = false;
    for (const g of EVA_MULTI_GLYPHS) {
      if (text.substring(i, i + g.length) === g) {
        tokens.push(g);
        i += g.length;
        matched = true;
        break;
      }
    }
    if (!matched) { tokens.push(text[i]); i++; }
  }
  return tokens;
}

function evaWords(evaText: string): string[] {
  return evaText
    .replace(/[\r\n]+/g, '.')
    .split('.')
    .map((w) => w.trim().toLowerCase())
    .filter((w) => w.length > 0 && /^[a-z]+$/.test(w));
}

// ---------------------------------------------------------------------------
// Crib candidates — well-known zodiac month label hypotheses
// from published Voynich transcription research (Zandbergen, d'Imperio et al.)
//
// Format: [EVA label, Latin month candidates]
//
// These are EVA words that appear in or near zodiac folios and whose token
// structure is plausibly consistent with a short month name:
//   - Length 2-5 multi-glyph tokens (like "marchyo" → "martius")
//   - Appear multiple times in zodiac folios
//
// Primary candidates from f70v-f73v star/nymph labels (Zandbergen 2022):
//   "otalchy" → "aries" / "march" (5 tokens: o-t-al-ch-y)
//   "otar"    → "aries" (2-3 tokens)
//   "otshy"   → possibly "taurus"
//   "otchedy" → possibly "gemini"
//   "otaiin"  → possibly variant label
// ---------------------------------------------------------------------------

interface CribPair {
  evaWord: string;         // EVA token sequence (joined, no dots)
  evaTokens: string[];     // tokenized
  latinCandidates: string[]; // possible Latin plaintext meanings
  source: string;          // where this crib came from
}

const KNOWN_CRIBS: CribPair[] = [
  // Month name cribs (from Voynich zodiac label scholarship)
  {
    evaWord: 'otalchy',
    evaTokens: tokenizeEva('otalchy'),
    latinCandidates: ['martius', 'march', 'aries'],
    source: 'zodiac f70v Aries label (Zandbergen)',
  },
  {
    evaWord: 'otar',
    evaTokens: tokenizeEva('otar'),
    latinCandidates: ['aries', 'april', 'aprilis'],
    source: 'zodiac Aries short label',
  },
  {
    evaWord: 'otchy',
    evaTokens: tokenizeEva('otchy'),
    latinCandidates: ['taurus', 'tauri', 'maius'],
    source: 'zodiac Taurus label hypothesis',
  },
  {
    evaWord: 'otshy',
    evaTokens: tokenizeEva('otshy'),
    latinCandidates: ['taurus', 'aprilis', 'maior'],
    source: 'zodiac Taurus/April label variant',
  },
  {
    evaWord: 'daiin',
    evaTokens: tokenizeEva('daiin'),
    latinCandidates: ['datum', 'datur', 'dicto', 'dixit'],
    source: 'high-frequency herbal word (content word crib)',
  },
  {
    evaWord: 'chedy',
    evaTokens: tokenizeEva('chedy'),
    latinCandidates: ['herba', 'causa', 'calor', 'cedri'],
    source: 'high-frequency herbal word',
  },
  {
    evaWord: 'qokeedy',
    evaTokens: tokenizeEva('qokeedy'),
    latinCandidates: ['quaerit', 'quomodo', 'quoddam'],
    source: 'common herbal EVA pattern (qo-prefix)',
  },
];

// ---------------------------------------------------------------------------
// Latin corpus for scoring
// ---------------------------------------------------------------------------

const LATIN_CORPUS = `
mandragora habet folia similia brassicae sed maiora et latiora atque pilosa
radix alba et longa cortice nigro et interior est alba velut rapum
nascitur in locis cultis et hortulanis folia in terra prostrata iacent
recipe folium mentae et tere cum aceto et impone super tempora dolenti
verbena herba sacra est et habet flores parvos et purpureos contra venenum
absinthium amarum est et tollit vermes et purgat hepar et lien et stomachum
artemisia mater herbarum dicitur et iuvat morbos matricis et menstrua provocat
melissa habet odorem suavem et confortat cor et oculos et virtutes vitales
recipe semen anethi et tere subtiliter et misce cum oleo et vino calido
folium hederae cum aceto bibitum tollit dolorem capitis et tussim purgat
plantago herba frigida et sicca est et confortat stomachum et sistit fluxum
betonica multarum virtutum herba est et sanat dolores capitis antiquos
herbarum omnium potentissima est et multas habet vires medicinales
`;

interface TrigramModel { ngrams: Map<string, number>; total: number; vocabSize: number; }

function buildTrigramModel(corpus: string): TrigramModel {
  const text = ` ${corpus.toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim()} `;
  const ngrams = new Map<string, number>();
  let total = 0;
  for (let i = 0; i < text.length - 2; i++) {
    const tg = text.slice(i, i + 3);
    ngrams.set(tg, (ngrams.get(tg) ?? 0) + 1);
    total++;
  }
  return { ngrams, total, vocabSize: ngrams.size };
}

const LATIN_MODEL = buildTrigramModel(LATIN_CORPUS);

function langModelScore(text: string): number {
  const clean = ` ${text.toLowerCase().replace(/[^a-z\s]/g, ' ').replace(/\s+/g, ' ').trim()} `;
  if (clean.length < 5) return 0;
  const { ngrams, total, vocabSize } = LATIN_MODEL;
  let logProbSum = 0;
  let count = 0;
  for (let i = 0; i < clean.length - 2; i++) {
    const tg = clean.slice(i, i + 3);
    logProbSum += Math.log(((ngrams.get(tg) ?? 0) + 1) / (total + vocabSize));
    count++;
  }
  if (count === 0) return 0;
  return Math.max(0, Math.min(1, (logProbSum / count - (-8.5)) / ((-5.5) - (-8.5))));
}

// ---------------------------------------------------------------------------
// Partial map from crib: EVA-token → Latin-char
// ---------------------------------------------------------------------------

/**
 * Given an EVA word (tokenized) and a Latin candidate,
 * derive a partial glyph→letter substitution map.
 *
 * Each EVA token maps to one Latin character. We align token-by-token.
 * If token count != char count: the crib is length-incompatible → skip.
 */
function deriveCribMap(evaTokens: string[], latin: string): Map<string, string> | null {
  if (evaTokens.length !== latin.length) return null;
  const map = new Map<string, string>();
  for (let i = 0; i < evaTokens.length; i++) {
    const existing = map.get(evaTokens[i]);
    if (existing && existing !== latin[i]) return null;  // contradiction
    map.set(evaTokens[i], latin[i]);
  }
  // Check for many-to-one collision (two EVA tokens mapping to same Latin char)
  const used = new Set<string>();
  for (const v of map.values()) {
    if (used.has(v)) return null;
    used.add(v);
  }
  return map;
}

// ---------------------------------------------------------------------------
// Apply partial map + random completion → full substitution
// ---------------------------------------------------------------------------

const LATIN_ALPHABET = 'etainsrolcumpdbghvfyxwkqjz'.split('');

function buildFullMap(
  partial: Map<string, string>,
  evaGlyphs: string[],
): Record<string, string> {
  const usedTargets = new Set(partial.values());
  const freeTargets = LATIN_ALPHABET.filter((c) => !usedTargets.has(c));
  const shuffled = [...freeTargets].sort(() => Math.random() - 0.5);
  const map: Record<string, string> = {};
  let freeIdx = 0;
  for (const g of evaGlyphs) {
    if (partial.has(g)) {
      map[g] = partial.get(g)!;
    } else if (!(g in map)) {
      map[g] = shuffled[freeIdx % shuffled.length];
      freeIdx++;
    }
  }
  return map;
}

function applyMap(evaText: string, map: Record<string, string>): string {
  const tokens = tokenizeEva(evaText);
  return tokens.map((t) => map[t] ?? t).join('');
}

// ---------------------------------------------------------------------------
// Crib-constrained SA hill-climbing
// ---------------------------------------------------------------------------

function runCribSA(
  evaCorpusTexts: string[],
  partial: Map<string, string>,
  evaGlyphs: string[],
  saSteps: number,
): { map: Record<string, string>; score: number } {
  const sampleText = evaCorpusTexts.slice(0, 5).join(' . ');

  let map = buildFullMap(partial, evaGlyphs);
  let bestScore = langModelScore(applyMap(sampleText, map));
  let bestMap = { ...map };

  const lockedGlyphs = new Set(partial.keys());
  const mutableGlyphs = evaGlyphs.filter((g) => !lockedGlyphs.has(g));

  for (let step = 0; step < saSteps; step++) {
    const temp = 0.3 * Math.exp(-4 * step / saSteps);
    const i = Math.floor(Math.random() * mutableGlyphs.length);
    const j = Math.floor(Math.random() * mutableGlyphs.length);
    if (i === j) continue;

    const gi = mutableGlyphs[i];
    const gj = mutableGlyphs[j];
    const vi = map[gi];
    const vj = map[gj];

    map[gi] = vj;
    map[gj] = vi;

    const newScore = langModelScore(applyMap(sampleText, map));
    const delta = newScore - bestScore;

    if (delta > 0 || Math.random() < Math.exp(delta / Math.max(temp, 1e-6))) {
      bestScore = newScore;
      if (bestScore > langModelScore(applyMap(sampleText, bestMap))) {
        bestMap = { ...map };
      }
    } else {
      map[gi] = vi;
      map[gj] = vj;
    }
  }

  return { map: bestMap, score: langModelScore(applyMap(sampleText, bestMap)) };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('crib-attack — zodiac month label cribs');
  console.log('');

  // 1. Check available sections
  const sectionRows = await executeSql(`
    SELECT section, COUNT(*) AS n
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    GROUP BY section ORDER BY n DESC
  `);
  const sections = sectionRows.map((r) => r.section ?? '(null)');
  console.log(`Available sections: ${sections.join(', ')}`);
  console.log('');

  // 2. Load zodiac/astronomical section if available; otherwise note fallback
  const zodiacSections = sections.filter((s) =>
    ['astronomical', 'zodiac', 'astrological', 'cosmological'].includes(s.toLowerCase())
  );
  let zodiacEvaTexts: string[] = [];

  if (zodiacSections.length > 0) {
    for (const sec of zodiacSections) {
      const rows = await executeSql(`
        SELECT folio_id, eva_text
        FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
        WHERE section = '${sec.replace(/'/g, "''")}'
      `);
      const texts = rows.filter((r) => r.eva_text).map((r) => r.eva_text as string);
      zodiacEvaTexts.push(...texts);
      console.log(`Loaded ${texts.length} folios from section '${sec}'`);
    }
    console.log('');

    // Extract short EVA words from zodiac folios — potential label candidates
    const zodiacWordFreqs = new Map<string, number>();
    for (const text of zodiacEvaTexts) {
      for (const w of evaWords(text)) {
        const tokens = tokenizeEva(w);
        if (tokens.length >= 2 && tokens.length <= 6) {
          zodiacWordFreqs.set(w, (zodiacWordFreqs.get(w) ?? 0) + 1);
        }
      }
    }
    const recurringLabels = [...zodiacWordFreqs.entries()]
      .filter(([, n]) => n >= 3)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 20);

    console.log('Recurring short words in zodiac folios (potential nymph labels):');
    for (const [word, count] of recurringLabels) {
      console.log(`  ${word.padEnd(15)} × ${count}  tokens: [${tokenizeEva(word).join(', ')}]`);
    }
    console.log('');
  } else {
    console.log('No astronomical/zodiac section found in eva_corpus.');
    console.log('Using published crib candidates from Voynich transcription research.');
    console.log('');
  }

  // 3. Load herbal corpus for SA scoring
  const herbalRows = await executeSql(`
    SELECT folio_id, eva_text
    FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus
    WHERE section = 'herbal'
    LIMIT 30
  `);
  const herbalTexts = herbalRows.filter((r) => r.eva_text).map((r) => r.eva_text as string);

  // Compute EVA glyph vocabulary from herbal corpus
  const glyphFreqs = new Map<string, number>();
  for (const text of herbalTexts) {
    for (const g of tokenizeEva(text)) {
      glyphFreqs.set(g, (glyphFreqs.get(g) ?? 0) + 1);
    }
  }
  const evaGlyphs = [...glyphFreqs.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([g]) => g);

  console.log(`EVA glyph vocabulary: ${evaGlyphs.length} distinct tokens`);
  console.log(`Top glyphs: ${evaGlyphs.slice(0, 12).join(', ')}`);
  console.log('');

  // 4. Run crib attack for each known crib pair
  console.log('=== Crib attack results ===');
  console.log('(SA_STEPS=2000 per restart, 3 restarts per crib pair)');
  console.log('');

  const SA_STEPS = 2000;
  const N_RESTARTS = 3;

  const results: Array<{
    crib: CribPair;
    latin: string;
    partial: Map<string, string>;
    bestScore: number;
    sampleDecode: string;
  }> = [];

  for (const crib of KNOWN_CRIBS) {
    for (const latin of crib.latinCandidates) {
      const partial = deriveCribMap(crib.evaTokens, latin);
      if (!partial) {
        console.log(`  ${crib.evaWord} → "${latin}" : SKIP (length mismatch: ${crib.evaTokens.length} tokens vs ${latin.length} chars)`);
        continue;
      }

      console.log(`  ${crib.evaWord} → "${latin}" (${crib.source})`);
      console.log(`    partial map: ${[...partial.entries()].map(([k, v]) => `${k}→${v}`).join(', ')}`);

      let bestScore = -Infinity;
      let bestMap: Record<string, string> = {};

      for (let r = 0; r < N_RESTARTS; r++) {
        const { map, score } = runCribSA(herbalTexts, partial, evaGlyphs, SA_STEPS);
        if (score > bestScore) {
          bestScore = score;
          bestMap = map;
        }
      }

      // Decode sample text
      const sample = herbalTexts[0] ?? '';
      const sampleDecode = applyMap(sample, bestMap).slice(0, 120);

      console.log(`    best score: ${bestScore.toFixed(4)} (gate: 0.30)`);
      console.log(`    sample: "${sampleDecode}"`);
      console.log('');

      results.push({ crib, latin, partial, bestScore, sampleDecode });
    }
  }

  // 5. Summary
  console.log('=== Summary ===');
  const sorted = results.sort((a, b) => b.bestScore - a.bestScore);
  console.log('Top 5 crib pairs by score:');
  for (const r of sorted.slice(0, 5)) {
    const flag = r.bestScore >= 0.30 ? '★' : ' ';
    console.log(`  ${flag} "${r.crib.evaWord}" → "${r.latin}" : score=${r.bestScore.toFixed(4)}`);
  }

  const promising = sorted.filter((r) => r.bestScore >= 0.30);
  if (promising.length > 0) {
    console.log(`\n${promising.length} crib pair(s) cleared the heuristic gate (score ≥ 0.30).`);
    console.log('Recommend: send best decode to critic agent for LLM judge.');
  } else {
    console.log('\nNo crib pair cleared the heuristic gate (all scores < 0.30).');
    console.log('Interpretation: if even correct cribs produce no Latin signal,');
    console.log('the substitution hypothesis is wrong even with constrained keys.');
  }
}

main().catch((err) => {
  console.error('[crib-attack] fatal:', err);
  process.exit(1);
});
