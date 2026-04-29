/**
 * One-off backfill: convert each folio's visual_description into a list of
 * Latin and Italian botanical terms via an LLM call, then write back to the
 * `expected_terms` column of folio_vision_analysis.
 *
 * Run from the orchestrator deploy directory:
 *   npx tsx backfill-expected-terms.ts
 *
 * Required env: DATABRICKS_HOST, DATABRICKS_WAREHOUSE_ID,
 * and either DATABRICKS_TOKEN or DATABRICKS_CLIENT_ID/SECRET.
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

const MODEL = process.env.MODEL ?? 'databricks-claude-sonnet-4-6';
const TABLE = 'serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis';

async function executeSql(statement: string): Promise<Array<Record<string, string>>> {
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
  const data = await res.json() as {
    result?: { data_array?: string[][] };
    manifest?: { schema?: { columns?: Array<{ name: string }> }; columns?: Array<{ name: string }> };
    status?: { state?: string; error?: { message?: string } };
  };
  if (data.status?.state === 'FAILED') throw new Error(`SQL failed: ${data.status.error?.message}`);
  const cols = (data.manifest?.schema?.columns ?? data.manifest?.columns ?? []).map((c) => c.name);
  const rows = data.result?.data_array ?? [];
  return rows.map((row) => {
    const obj: Record<string, string> = {};
    cols.forEach((c, i) => { obj[c] = row[i]; });
    return obj;
  });
}

async function callModel(prompt: string): Promise<string> {
  const host = resolveHost();
  const token = await resolveToken();
  const res = await fetch(`${host}/serving-endpoints/${MODEL}/invocations`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      messages: [{ role: 'user', content: prompt }],
      max_tokens: 1500,
      temperature: 0.2,
    }),
  });
  if (!res.ok) throw new Error(`Model ${res.status}: ${await res.text()}`);
  const data = await res.json() as {
    choices?: Array<{ message?: { content?: string } }>;
  };
  return data.choices?.[0]?.message?.content ?? '';
}

const PROMPT_TEMPLATE = (folio: string, plantName: string, plantLatin: string, description: string) => `You are a medieval botanical lexicographer. Given the description below of a plant illustrated in a 15th-century herbal manuscript, produce a JSON object listing botanical/anatomical/descriptive terms a medieval herbalist scribe might use when writing about this plant.

Folio: ${folio}
Identified as: ${plantName} (${plantLatin || 'unknown binomial'})
Description: ${description}

For each language, include 30-50 single-word lowercase terms, mixing:
- Plant identification (genus, species, common medieval names — only if confidently identified)
- Anatomical parts visible (radix/radice, folia/foglia, flos/fiore, semen/seme, fructus/frutto, caulis/gambo, etc.)
- Color terms matching what's depicted (alba/bianco, nigra/nero, rubra/rosso, viridis/verde, caerulea/azzurro, lutea/giallo, purpurea/porpora, etc.)
- Shape/texture descriptors implied by the drawing (longa/lungo, lata/largo, acuta/acuto, pilosa/peloso, spinosa/spinoso, rotunda/rotondo, serrata/seghettato, etc.)
- General herbal vocabulary plausible for this plant (herba/erba, planta/pianta, virtus/virtù, sanat/sana, etc.)

Use authentic medieval Latin (nominative singular nouns, feminine singular adjectives, basic verb forms) and trecento Italian (vernacular forms — "et" not "and", "vino" / "olio" / "miele").
Avoid duplicates and avoid English words.

Return STRICT JSON with no markdown, no explanation, exactly this shape:
{"latin":["...","..."],"italian":["...","..."]}`;

async function backfillOne(row: { folio_id: string; visual_description: string; plant_name: string; plant_latin: string }): Promise<{ latin: string[]; italian: string[] } | null> {
  const prompt = PROMPT_TEMPLATE(row.folio_id, row.plant_name, row.plant_latin, row.visual_description);
  const raw = await callModel(prompt);
  const jsonStart = raw.indexOf('{');
  const jsonEnd = raw.lastIndexOf('}');
  if (jsonStart === -1 || jsonEnd === -1) {
    console.warn(`[${row.folio_id}] no JSON in response: ${raw.slice(0, 100)}`);
    return null;
  }
  try {
    const parsed = JSON.parse(raw.slice(jsonStart, jsonEnd + 1)) as { latin?: string[]; italian?: string[] };
    const latin = (parsed.latin ?? []).map((t) => t.toLowerCase().trim()).filter(Boolean);
    const italian = (parsed.italian ?? []).map((t) => t.toLowerCase().trim()).filter(Boolean);
    return { latin, italian };
  } catch (err) {
    console.warn(`[${row.folio_id}] parse failed:`, err);
    return null;
  }
}

async function main() {
  console.log(`[backfill] loading folios from ${TABLE}...`);
  const folios = await executeSql(`
    SELECT folio_id, visual_description, subject_candidates
    FROM ${TABLE}
    WHERE section = 'herbal'
      AND visual_description IS NOT NULL
      AND LENGTH(visual_description) > 100
    ORDER BY folio_id
  `);
  console.log(`[backfill] ${folios.length} folios to process`);

  let okCount = 0;
  let failCount = 0;
  for (const f of folios) {
    const candidates = JSON.parse(f.subject_candidates || '[]');
    const top = candidates[0] ?? {};
    try {
      const result = await backfillOne({
        folio_id: f.folio_id,
        visual_description: f.visual_description,
        plant_name: top.name ?? 'unknown',
        plant_latin: top.latin ?? '',
      });
      if (!result) { failCount++; continue; }
      const json = JSON.stringify(result).replace(/'/g, "''");
      await executeSql(`
        UPDATE ${TABLE}
        SET expected_terms = '${json}'
        WHERE folio_id = '${f.folio_id}' AND section = 'herbal'
      `);
      console.log(`[backfill] ${f.folio_id}: latin=${result.latin.length}, italian=${result.italian.length} terms — sample: ${result.latin.slice(0, 5).join(', ')}`);
      okCount++;
    } catch (err) {
      console.warn(`[backfill] ${f.folio_id} failed:`, err);
      failCount++;
    }
  }
  console.log(`[backfill] done — ok=${okCount}, fail=${failCount}`);
}

main().catch((err) => {
  console.error('[backfill] fatal:', err);
  process.exit(1);
});
