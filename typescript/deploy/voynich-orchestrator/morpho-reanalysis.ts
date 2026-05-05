/**
 * Morphological re-analysis for low-confidence Voynich herbal folios.
 *
 * For folios where vision confidence < MIN_CONF (default 0.35), fetches
 * the IIIF image and runs a morphology-focused prompt that identifies plant
 * family from structural features (root shape, leaf morphology, flower/fruit
 * type) rather than gestalt appearance. Fantastical plants can still be
 * classified morphologically — the Voynich illustrator consistently drew
 * solanaceae-type branched/forked roots even on composite fantasy images.
 *
 * Updates subject_candidates, botanical_features, visual_description, and
 * prompt_version in folio_vision_analysis. Does NOT touch expected_terms.
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx morpho-reanalysis.ts
 *
 * Env vars:
 *   MIN_CONF=0.35        re-analyze folios where top confidence < this value
 *   TARGET_FOLIOS=       comma-separated folio IDs to restrict (e.g. f112r,f58r,f103v)
 *   MAX_FOLIOS=60        max folios to process per run
 *   RATE_LIMIT_MS=3000   delay between LLM calls
 *   DRY_RUN=false        if 'true', print plan without writing to DB
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const MIN_CONF      = parseFloat(process.env.MIN_CONF ?? '0.35');
const MAX_FOLIOS    = parseInt(process.env.MAX_FOLIOS ?? '60');
const RATE_LIMIT_MS = parseInt(process.env.RATE_LIMIT_MS ?? '3000');
const DRY_RUN       = process.env.DRY_RUN === 'true';
const MODEL_ID      = process.env.VISION_MODEL ?? 'databricks-claude-sonnet-4-6';

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

async function executeSqlWrite(statement: string): Promise<void> {
  const host = resolveHost();
  const token = await resolveToken();
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID not set');
  const res = await fetch(`${host}/api/2.0/sql/statements`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ warehouse_id: warehouseId, statement, wait_timeout: '50s' }),
  });
  if (!res.ok) throw new Error(`SQL write ${res.status}: ${await res.text()}`);
  const data = (await res.json()) as { status?: { state?: string; error?: { message?: string } } };
  if (data.status?.state === 'FAILED') throw new Error(`SQL write failed: ${data.status.error?.message}`);
}

// ---------------------------------------------------------------------------
// Morphology-focused LLM prompt
// ---------------------------------------------------------------------------

const MORPHO_PROMPT = `You are analyzing a page from the Voynich manuscript (MS 408, Yale Beinecke Library), a 15th-century illustrated herbal manuscript.

A previous analysis could not confidently identify the plant on this folio. Your task is a MORPHOLOGICAL ANALYSIS — focus on the physical structure of the illustrated plant regardless of whether it looks realistic or fantastical. The illustrator consistently rendered morphological features (root branching patterns, leaf shape, flower structure) even in composite or stylized images.

Examine and describe each of these structural features:

1. ROOT system: Is the root branched/forked? Does it resemble a human figure (mandrake-type anthropomorphic root)? Single taproot, fibrous mass, bulbous, or complex branched system?
2. STEM: Herbaceous or woody? Erect, trailing, or branching? Are there nodes, hairs, or other features?
3. LEAVES: Shape (lanceolate, ovate, cordate, pinnate, palmate), margin (entire, serrate, lobed, deeply-cut), arrangement (alternate, opposite, basal rosette, whorled), texture (hairy/pubescent, smooth/glabrous)?
4. FLOWERS (if visible): Number of petals, shape (bell-shaped, star-shaped, tubular, composite head with ray florets), color, arrangement?
5. FRUITS or SEEDS (if visible): Berry, capsule, achene, or other?

Based ONLY on the morphological features you observe, assign the plant to the family it MOST closely resembles:
- Solanaceae (mandrake, nightshade, henbane): characteristic features include forked/branched root (especially anthropomorphic in mandrake), hairy ovate to lanceolate leaves, 5-petaled star or bell-shaped flowers, berry-type fruit
- Thistle/Asteraceae (carduus, cirsium): spiny or deeply-lobed leaves, composite flower head with many small florets and ray petals, achene fruits, no obvious fleshy root
- Plantain/Plantaginaceae (plantago): parallel-veined basal rosette leaves, slender spike flower, fibrous root
- Poppy/Papaveraceae (papaver): deeply-lobed or dissected leaves, 4-petaled flowers, rounded capsule fruit
- Other: if features match a different identifiable family, name it
- Unidentifiable: if fewer than 2 distinctive morphological features are visible, say so honestly

Return ONLY a JSON object with exactly these fields:
{
  "subject_candidates": [
    {
      "name": "Identification based on morphology (e.g. 'Mandrake / Mandragora')",
      "latin": "Genus species or family name",
      "confidence": 0.0-1.0,
      "reasoning": "List the specific morphological features that support this assignment"
    }
  ],
  "botanical_features": ["specific", "morphological", "features", "observed", "as", "short", "phrases"],
  "visual_description": "2-3 sentences focused on the morphological structure of the illustration"
}

Calibrate confidence carefully:
- < 0.20: fewer than 2 diagnostic features visible
- 0.20–0.35: suggestive features present but ambiguous
- 0.35–0.55: 2+ diagnostic features clearly consistent with one family
- 0.55–0.75: strong morphological match with multiple diagnostic features
- > 0.75: unambiguous morphological identification

Return ONLY the JSON object, no other text.`;

interface MorphoResult {
  subject_candidates: Array<{ name: string; latin: string; confidence: number; reasoning: string }>;
  botanical_features: string[];
  visual_description: string;
}

// ---------------------------------------------------------------------------
// LLM call
// ---------------------------------------------------------------------------

async function analyzeMorphology(imageUrl: string): Promise<MorphoResult | null> {
  const host = resolveHost();
  const token = await resolveToken();

  const imgRes = await fetch(imageUrl);
  if (!imgRes.ok) throw new Error(`Image fetch ${imgRes.status}: ${imageUrl}`);
  const imgBuffer = await imgRes.arrayBuffer();
  const dataUrl = `data:image/jpeg;base64,${Buffer.from(imgBuffer).toString('base64')}`;

  const res = await fetch(`${host}/serving-endpoints/${MODEL_ID}/invocations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      messages: [{
        role: 'user',
        content: [
          { type: 'image_url', image_url: { url: dataUrl } },
          { type: 'text', text: MORPHO_PROMPT },
        ],
      }],
      max_tokens: 1000,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    console.warn(`    LLM ${res.status}: ${body.slice(0, 200)}`);
    return null;
  }

  const data = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
  const content = data.choices?.[0]?.message?.content ?? '';
  try {
    const cleaned = content.replace(/```json?\s*/g, '').replace(/```/g, '').trim();
    return JSON.parse(cleaned) as MorphoResult;
  } catch {
    console.warn(`    JSON parse failed. Raw: ${content.slice(0, 200)}`);
    return null;
  }
}

// ---------------------------------------------------------------------------
// DB update
// ---------------------------------------------------------------------------

function escape(s: string): string {
  return s.replace(/'/g, "''");
}

async function updateFolio(folioId: string, result: MorphoResult): Promise<void> {
  const sc = escape(JSON.stringify(result.subject_candidates));
  const bf = escape(JSON.stringify(result.botanical_features));
  const vd = escape(result.visual_description);

  await executeSqlWrite(`
    UPDATE serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    SET
      subject_candidates = '${sc}',
      botanical_features = '${bf}',
      visual_description = '${vd}',
      prompt_version     = 'v2-morpho',
      analyzed_at        = current_timestamp()
    WHERE folio_id = '${escape(folioId)}' AND section = 'herbal'
  `);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log('morpho-reanalysis — morphological second-pass for low-confidence herbal folios');
  console.log(`Model: ${MODEL_ID}, MIN_CONF=${MIN_CONF}, MAX_FOLIOS=${MAX_FOLIOS}, DRY_RUN=${DRY_RUN}`);
  if (TARGET_FOLIOS) console.log(`Targeting: ${[...TARGET_FOLIOS].join(', ')}`);
  console.log('');

  // Load all herbal folios with current classification
  console.log('Loading folio_vision_analysis...');
  const rows = await executeSql(`
    SELECT folio_id, image_url, subject_candidates
    FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis
    WHERE section = 'herbal'
    ORDER BY folio_id
  `);

  interface FolioRecord {
    folioId: string;
    imageUrl: string;
    topName: string;
    topConf: number;
    promptVersion: string;
  }

  const records: FolioRecord[] = [];
  for (const r of rows) {
    const folioId = r.folio_id ?? '';
    const imageUrl = r.image_url ?? '';
    if (!folioId || !imageUrl) continue;

    let topName = 'unknown';
    let topConf = 0;
    try {
      const cands = JSON.parse(r.subject_candidates ?? '[]') as Array<{ name?: string; confidence?: number }>;
      topName = cands[0]?.name ?? 'unknown';
      topConf = cands[0]?.confidence ?? 0;
    } catch { /* leave defaults */ }

    records.push({ folioId, imageUrl, topName, topConf, promptVersion: r.prompt_version ?? 'v1' });
  }

  console.log(`  ${records.length} herbal folios loaded`);

  // Filter: low confidence, and optionally restricted to TARGET_FOLIOS
  const todo = records
    .filter((r) => r.topConf < MIN_CONF)
    .filter((r) => !TARGET_FOLIOS || TARGET_FOLIOS.has(r.folioId))
    .slice(0, MAX_FOLIOS);

  console.log(`  ${todo.length} folios to re-analyze (conf < ${MIN_CONF}${TARGET_FOLIOS ? ', targeted' : ''})`);
  console.log('');

  if (todo.length === 0) {
    console.log('Nothing to do.');
    return;
  }

  // Print plan
  console.log('Plan:');
  for (const r of todo) {
    console.log(`  ${r.folioId.padEnd(10)} conf=${r.topConf.toFixed(2)}  "${r.topName.slice(0, 60)}"`);
  }
  console.log('');

  if (DRY_RUN) {
    console.log('DRY_RUN=true — no writes. Re-run without DRY_RUN to execute.');
    return;
  }

  // Process
  let upgraded = 0, unchanged = 0, failed = 0;

  for (let i = 0; i < todo.length; i++) {
    const r = todo[i];
    process.stdout.write(`[${i + 1}/${todo.length}] ${r.folioId} (was ${r.topConf.toFixed(2)}) `);

    try {
      const result = await analyzeMorphology(r.imageUrl);
      if (!result) {
        process.stdout.write('FAILED (parse error)\n');
        failed++;
      } else {
        const newConf = result.subject_candidates?.[0]?.confidence ?? 0;
        const newName = result.subject_candidates?.[0]?.name ?? 'unknown';
        const newLatin = result.subject_candidates?.[0]?.latin ?? '';
        const delta = newConf - r.topConf;
        const arrow = delta > 0.01 ? '↑' : delta < -0.01 ? '↓' : '=';

        await updateFolio(r.folioId, result);

        if (newConf >= MIN_CONF) {
          process.stdout.write(`→ ${newConf.toFixed(2)} ${arrow}  ${newName} / ${newLatin}  *** RECLASSIFIED ***\n`);
          upgraded++;
        } else {
          process.stdout.write(`→ ${newConf.toFixed(2)} ${arrow}  ${newName} (still below gate)\n`);
          unchanged++;
        }
      }
    } catch (err) {
      process.stdout.write(`ERROR: ${(err as Error).message.slice(0, 80)}\n`);
      failed++;
    }

    if (i < todo.length - 1) await new Promise((r) => setTimeout(r, RATE_LIMIT_MS));
  }

  console.log('');
  console.log('=== DONE ===');
  console.log(`  Reclassified (conf ≥ ${MIN_CONF}): ${upgraded}`);
  console.log(`  Still low-confidence:               ${unchanged}`);
  console.log(`  Failed:                              ${failed}`);
  console.log('');
  if (upgraded > 0) {
    console.log('Re-run anchor-loop.ts to see if any new anchors pass the gate.');
  }
}

main().catch((err) => {
  console.error('[morpho-reanalysis] fatal:', err);
  process.exit(1);
});
