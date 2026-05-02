/**
 * Richer expected_terms re-extraction — replaces the generic backfill vocabulary
 * with family-aware, pharmacologically-complete medieval herbal terms.
 *
 * The original backfill asked for anatomical parts, colors, and shapes. It missed:
 *   - Family-specific identification names (mandragoras, hyoscyamus, carduus…)
 *   - Pharmacological properties (narcoticum, soporificum, anodynum, purgativum…)
 *   - Preparation forms (infusum, decoctum, electuarium, emplastrum…)
 *   - Body targets (caput, stomachus, uterus, viscera, dolor, febris…)
 *
 * These omissions starve PMI of the signal that would link EVA words on
 * solanaceae pages to solanaceae-relevant vocabulary. This script re-extracts
 * with a much richer prompt calibrated to each plant's identified family.
 *
 * Delta Lake time travel (7-day retention) provides rollback safety.
 * Old terms are logged to stdout before overwrite.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx richer-expected-terms.ts
 *
 * Env vars:
 *   TARGET_FOLIOS=       comma-separated folio IDs to restrict (e.g. f75r,f88r)
 *   MAX_FOLIOS=250       max folios to process per run
 *   RATE_LIMIT_MS=3000   delay between LLM calls
 *   DRY_RUN=false        if 'true', print plan without writing to DB
 *   MIN_CONF=0.35        only re-extract folios where top candidate confidence >= this
 *                        (set to 0 to process all folios regardless of vision confidence)
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const MODEL         = process.env.MODEL          ?? 'databricks-claude-sonnet-4-6';
const TABLE         = 'serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis';
const MAX_FOLIOS    = parseInt(process.env.MAX_FOLIOS    ?? '250');
const RATE_LIMIT_MS = parseInt(process.env.RATE_LIMIT_MS ?? '3000');
const DRY_RUN       = process.env.DRY_RUN === 'true';
const MIN_CONF      = parseFloat(process.env.MIN_CONF    ?? '0.35');

const TARGET_FOLIOS: Set<string> | null = process.env.TARGET_FOLIOS
  ? new Set(process.env.TARGET_FOLIOS.split(',').map((s) => s.trim()).filter(Boolean))
  : null;

// ---------------------------------------------------------------------------
// SQL helpers
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
// Plant family detection (mirrors pmi-analysis.ts classifyFamily)
// ---------------------------------------------------------------------------

function detectFamily(subjectCandidates: string | null): string {
  let cands: Array<{ name?: string; latin?: string; confidence?: number }> = [];
  try { cands = JSON.parse(subjectCandidates ?? '[]'); } catch { return 'unknown'; }
  const top = cands[0];
  if (!top || (top.confidence ?? 0) < MIN_CONF) return 'uncertain';
  const latin = (top.latin ?? '').toLowerCase();
  const name  = (top.name  ?? '').toLowerCase();
  if (/mandragora|mandrake|solanum|atropa|hyoscyamus|nightshade|henbane/.test(latin + name)) return 'solanaceae';
  if (/cirsium|carduus|thistle|carlina|centaurea|teasel/.test(latin + name))                 return 'thistle';
  if (/plantago|plantain/.test(latin + name))                                                return 'plantago';
  if (/papaver|poppy/.test(latin + name))                                                    return 'poppy';
  if (/artemisia|absinthium|wormwood|mugwort|tansy/.test(latin + name))                      return 'artemisia';
  if (/borago|borage/.test(latin + name))                                                    return 'borago';
  if (/nymphaea|water.?lily|aquatic/.test(latin + name))                                     return 'aquatic';
  if (/not.*herbal|astronomical|zodiac|cosmological|balneological|bathing/.test(name))       return 'non-botanical';
  return 'other';
}

// ---------------------------------------------------------------------------
// Family-specific vocabulary injections for the prompt
// ---------------------------------------------------------------------------

const FAMILY_VOCAB: Record<string, string> = {
  solanaceae: `For solanaceae/mandrake/nightshade family specifically include:
   - ID names: mandragoras, mandragora, hyoscyamus, belladona, stramonium, solanum, herba morella, alruna
   - Pharmacological: narcoticum, soporificum, anodynum, stupefactivum, sedativum, somnificum, analgesicum
   - Preparation: unguentum soporis, oleum, vinum mandragorae, succo
   - Uses: dolor, somnus, insomnia, furor, ardor, morbus, convulsio, epilepsia, febris`,

  thistle: `For thistle/asteraceae family specifically include:
   - ID names: carduus, cirsium, spina, acanthus, carlina, centaurea, cnicus, cardus benedictus
   - Pharmacological: sudorificum, diaphoreticum, febrifugum, cholagogum, stomachicum, amarum
   - Preparation: decoctum, infusum, succo, pulvis, herba benedicta
   - Uses: febris, icterus, iecur, stomachus, lien, calculus, vulnus`,

  plantago: `For plantago/plantain family specifically include:
   - ID names: plantago, psyllium, lanceolata, ribwort, arnoglossa, herba coronaria
   - Pharmacological: astringens, vulnerarium, refrigerans, diuretica, expectorans
   - Preparation: succo, decoctum, emplastrum, cataplasma
   - Uses: vulnus, ulcus, oculus, renes, vesica, tussim, pulmones`,

  poppy: `For poppy/papaveraceae family specifically include:
   - ID names: papaver, opium, meconium, papaver somniferum, papaver rubrum
   - Pharmacological: narcoticum, soporificum, anodynum, analgesicum, sedativum, stupefactivum, lachrima papaveris
   - Preparation: opium, succo, decoctum, capsula, electuarium
   - Uses: dolor, somnus, insomnia, tussim, dissenteria, febris`,

  artemisia: `For artemisia/wormwood/mugwort family specifically include:
   - ID names: artemisia, absinthium, matricaria, tanacetum, chrysanthemum, herba sancti johannis
   - Pharmacological: amarum, stomachicum, emenagogum, anthelminthicum, febrifugum, carminativum
   - Preparation: infusum, decoctum, vinum, succo, aqua
   - Uses: uterus, menses, stomachus, vermes, febris, viscera`,

  borago: `For borago/borage family specifically include:
   - ID names: borago, borago officinalis, borrago, buglossa, herba buglossa
   - Pharmacological: cordiale, laetificans, antidepressivum, sudorificum, diuretica, expectorans
   - Preparation: decoctum, succo, vinum, flos confectus
   - Uses: cor, melancholia, spiritus, febris, pulmones, renes`,

  aquatic: `For aquatic plants specifically include:
   - ID names: nymphaea, nenuphar, lotus, caltha, iris aquatica, juncus
   - Pharmacological: refrigerans, sedativum, astringens, anaphrodisiacum
   - Preparation: decoctum, succo, radix, semen
   - Uses: ardor, libido, febris, inflammatio, dolor capitis`,

  other: `Include pharmacological and preparation vocabulary relevant to the identified plant family.`,
  uncertain: `Include generic medieval herbal pharmacological vocabulary:
   - Properties: narcoticum, vulnerarium, astringens, diuretica, stomachicum, febrifugum
   - Preparations: decoctum, infusum, emplastrum, unguentum, pulvis, succo
   - Targets: stomachus, caput, uterus, cor, pulmones, iecur`,
};

// ---------------------------------------------------------------------------
// Richer prompt
// ---------------------------------------------------------------------------

function buildPrompt(folio: string, plantName: string, plantLatin: string, family: string, description: string): string {
  const familyGuidance = FAMILY_VOCAB[family] ?? FAMILY_VOCAB.uncertain;

  return `You are a medieval botanical lexicographer specializing in 15th-century materia medica. \
Given the plant identification below from the Voynich manuscript, produce a comprehensive JSON list of \
Latin and Italian terms a medieval herbalist scribe would write when describing and prescribing this plant.

Folio: ${folio}
Identified as: ${plantName} (${plantLatin || 'unknown binomial'})
Plant family: ${family}
Visual description: ${description}

${familyGuidance}

REQUIRED VOCABULARY CATEGORIES — include terms from ALL of these:

1. IDENTIFICATION NAMES: genus, species, medieval common names in Latin and Italian for this specific plant
2. PHARMACOLOGICAL PROPERTIES: narcoticum, soporificum, anodynum, vulnerarium, astringens, diuretica, purgativum, \
stomachicum, febrifugum, carminativum, cordiale, expectorans, and other action words
3. PREPARATION FORMS: infusum, decoctum, electuarium, emplastrum, unguentum, syrupus, pulvis, succus, vinum, oleum \
— Italian: infuso, decotto, cataplasma, sciroppo, unguento, polvere, succo, vino, olio
4. BODY TARGETS: caput, stomachus, uterus, cor, iecur, pulmo, renes, vesica, viscera, oculus, dolor, febris, vulnus, morbus, tumor
5. ANATOMICAL PARTS VISIBLE: radix, caulis, folium, flos, fructus, semen, cortex, fibra — Italian equivalents
6. COLORS and SHAPES: alba, nigra, rubra, viridis, lutea, purpurea — longa, rotunda, acuta, pilosa, spinosa, serrata

Produce 30–45 terms per language. CRITICAL: Every entry must be a SINGLE WORD (no spaces — write "mandragoras" not "mandragora officinarum"). Use authentic medieval Latin (nominative singular, feminine adjectives) and trecento Italian (vernacular). No English words. No duplicates.

Return ONLY valid JSON with no markdown, no explanation:
{"latin":["...","..."],"italian":["...","..."]}`;
}

// ---------------------------------------------------------------------------
// LLM call
// ---------------------------------------------------------------------------

async function callModel(prompt: string): Promise<string> {
  const host = resolveHost();
  const token = await resolveToken();
  const res = await fetch(`${host}/serving-endpoints/${MODEL}/invocations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      messages: [{ role: 'user', content: prompt }],
      max_tokens: 3000,
      temperature: 0.2,
    }),
  });
  if (!res.ok) throw new Error(`Model ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as { choices?: Array<{ message?: { content?: unknown } }> };
  const content = data.choices?.[0]?.message?.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    // Claude content-blocks format: [{type:'text', text:'...'}]
    return content.map((b) => (typeof (b as {text?: string}).text === 'string' ? (b as {text: string}).text : '')).join('');
  }
  return '';
}

function parseTerms(raw: string): { latin: string[]; italian: string[] } | null {
  const jsonStart = raw.indexOf('{');
  const jsonEnd   = raw.lastIndexOf('}');
  const toArr = (v: unknown) =>
    Array.isArray(v) ? v.map((t) => String(t).toLowerCase().trim()).filter(Boolean) : [];

  // Happy path: complete JSON
  if (jsonStart !== -1 && jsonEnd !== -1) {
    try {
      const parsed = JSON.parse(raw.slice(jsonStart, jsonEnd + 1)) as { latin?: unknown; italian?: unknown };
      return { latin: toArr(parsed.latin), italian: toArr(parsed.italian) };
    } catch { /* fall through to partial recovery */ }
  }

  // Partial-JSON recovery: response truncated mid-array — extract complete quoted strings
  const extractPartialArray = (key: string): string[] => {
    const match = raw.match(new RegExp(`"${key}":\\s*\\[([^\\]]*)`));
    if (!match) return [];
    const fragment = match[1];
    // Collect all complete quoted strings (last item may be truncated — drop it)
    const items: string[] = [];
    const re = /"((?:[^"\\]|\\.)*)"/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(fragment)) !== null) items.push(m[1].toLowerCase().trim());
    return items.filter(Boolean);
  };

  const latin   = extractPartialArray('latin');
  const italian = extractPartialArray('italian');
  if (latin.length === 0 && italian.length === 0) return null;
  return { latin, italian };
}

function escape(s: string): string {
  return s.replace(/'/g, "''");
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('richer-expected-terms — pharmacologically-complete vocabulary re-extraction');
  console.log(`Model: ${MODEL}  MIN_CONF=${MIN_CONF}  MAX_FOLIOS=${MAX_FOLIOS}  DRY_RUN=${DRY_RUN}`);
  if (TARGET_FOLIOS) console.log(`Targeting: ${[...TARGET_FOLIOS].join(', ')}`);
  console.log('');

  console.log('Loading folio_vision_analysis...');
  const rows = await executeSql(`
    SELECT folio_id, visual_description, subject_candidates, expected_terms
    FROM ${TABLE}
    WHERE section = 'herbal'
      AND visual_description IS NOT NULL
      AND LENGTH(visual_description) > 100
    ORDER BY folio_id
  `);

  interface FolioRecord {
    folioId: string;
    visualDescription: string;
    plantName: string;
    plantLatin: string;
    family: string;
    oldTerms: string | null;
  }

  const records: FolioRecord[] = [];
  for (const r of rows) {
    const folioId = r.folio_id ?? '';
    if (!folioId) continue;
    if (TARGET_FOLIOS && !TARGET_FOLIOS.has(folioId)) continue;

    let cands: Array<{ name?: string; latin?: string; confidence?: number }> = [];
    try { cands = JSON.parse(r.subject_candidates ?? '[]'); } catch { /* leave empty */ }
    const top = cands[0] ?? {};
    const family = detectFamily(r.subject_candidates);

    records.push({
      folioId,
      visualDescription: r.visual_description ?? '',
      plantName: top.name ?? 'unknown',
      plantLatin: top.latin ?? '',
      family,
      oldTerms: r.expected_terms ?? null,
    });
  }

  const todo = records.slice(0, MAX_FOLIOS);

  console.log(`  ${records.length} eligible folios, processing up to ${MAX_FOLIOS}`);
  console.log('');

  // Print plan
  const familyCounts: Record<string, number> = {};
  for (const r of todo) {
    familyCounts[r.family] = (familyCounts[r.family] ?? 0) + 1;
  }
  console.log('Family breakdown:');
  for (const [fam, cnt] of Object.entries(familyCounts).sort((a, b) => b[1] - a[1])) {
    console.log(`  ${fam.padEnd(20)} ${cnt}`);
  }
  console.log('');

  if (DRY_RUN) {
    console.log('DRY_RUN=true — no writes. Re-run without DRY_RUN to execute.');
    return;
  }

  let ok = 0, fail = 0;

  for (let i = 0; i < todo.length; i++) {
    const r = todo[i];
    process.stdout.write(`[${i + 1}/${todo.length}] ${r.folioId} (${r.family}) `);

    const prompt = buildPrompt(r.folioId, r.plantName, r.plantLatin, r.family, r.visualDescription);

    try {
      const raw = await callModel(prompt);
      const result = parseTerms(raw);
      if (!result) {
        process.stdout.write(`FAILED (parse error) — raw: ${raw.slice(0, 80)}\n`);
        fail++;
      } else {
        // Log old terms before overwrite (Delta Lake time travel is the hard backup)
        const oldLatin = (() => {
          try {
            const parsed = JSON.parse(r.oldTerms ?? '{}') as { latin?: string[] };
            return parsed.latin?.slice(0, 6).join(', ') ?? '—';
          } catch { return '—'; }
        })();

        const json = escape(JSON.stringify(result));
        await executeSql(`
          UPDATE ${TABLE}
          SET expected_terms = '${json}'
          WHERE folio_id = '${escape(r.folioId)}' AND section = 'herbal'
        `);

        process.stdout.write(
          `OK — ${result.latin.length}L/${result.italian.length}I terms  ` +
          `[was: ${oldLatin}]  [now: ${result.latin.slice(0, 5).join(', ')}]\n`
        );
        ok++;
      }
    } catch (err) {
      process.stdout.write(`ERROR: ${(err as Error).message.slice(0, 80)}\n`);
      fail++;
    }

    if (i < todo.length - 1) await new Promise((r) => setTimeout(r, RATE_LIMIT_MS));
  }

  console.log('');
  console.log('=== DONE ===');
  console.log(`  OK:     ${ok}`);
  console.log(`  Failed: ${fail}`);
  console.log('');
  console.log('Next: re-run pmi-analysis.ts then anchor-loop.ts to check gate impact.');
}

main().catch((err) => {
  console.error('[richer-expected-terms] fatal:', err);
  process.exit(1);
});
