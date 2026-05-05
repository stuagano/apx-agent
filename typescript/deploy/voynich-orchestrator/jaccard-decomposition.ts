/**
 * Jaccard family-signal decomposition: label tier vs. text tier.
 *
 * label-word-test.ts found 13 statistically significant label words
 * (once_rate ≥ 0.70, family-concentrated). family-distance-test.ts found
 * that EVA vocabulary clusters by botanical family (within-family Jaccard lift).
 *
 * This test asks: how much of that Jaccard family lift is carried by the
 * label tier vs. the text tier?
 *
 * Three vocabulary conditions per folio:
 *   FULL   — all EVA words (baseline)
 *   TEXT   — label words removed (text tier only)
 *   LABEL  — only the 13 label words retained (label tier only)
 *
 * For each condition:
 *   - Mean within-family Jaccard (same botanical family pairs)
 *   - Mean between-family Jaccard (different botanical family pairs)
 *   - Lift = within / between
 *
 * Also: 5-bin distance breakdown for FULL vs TEXT to see whether removing
 * labels changes the distance-decay profile.
 *
 * Interpretation:
 *   TEXT lift ≈ FULL lift → text tier independently encodes botanical family
 *   TEXT lift ≈ 1.0       → family signal is entirely carried by label words
 *   Both high             → two independent signals (label + text)
 *
 * Run:
 *   export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
 *   export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
 *   export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
 *   npx tsx jaccard-decomposition.ts
 */

import { resolveHost, resolveToken } from './appkit-agent/index.mjs';

// The 13 statistically significant label words from label-word-test.ts
const LABEL_WORDS = new Set([
  // solanaceae (p<0.05)
  'olaiin', 'qokeody', 'qockhey', 'ckhey', 'sy', 'oldaiin', 'cphey', 'okor', 'qockhol', 'qokan',
  // thistle (p<0.05)
  'kchy', 'ydaiin', 'ty',
]);

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

const BOTANICAL = new Set(['solanaceae', 'thistle', 'plantago', 'poppy', 'rose',
  'mint-family', 'ranunculaceae', 'apiaceae', 'verbena', 'artemisia', 'brassicaceae', 'lily-family']);

function extractWords(evaText: string): Set<string> {
  return new Set(
    evaText.replace(/[\r\n]+/g, ' ').split(/[\s.]+/)
      .map((w) => w.trim().toLowerCase())
      .filter((w) => w.length >= 2 && /^[a-z]+$/.test(w)),
  );
}

function jaccard(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 && b.size === 0) return 0;
  let inter = 0;
  for (const w of a) if (b.has(w)) inter++;
  const union = a.size + b.size - inter;
  return union === 0 ? 0 : inter / union;
}

function folioToSortKey(folioId: string): number {
  const m = folioId.match(/^f(\d+)(r|v)(\d?)$/i);
  if (!m) return 999999;
  return parseInt(m[1], 10) * 100 + (m[2].toLowerCase() === 'v' ? 1 : 0) * 10 + (m[3] ? parseInt(m[3], 10) : 0);
}

function quantileBreakpoints(values: number[], n: number): number[] {
  const sorted = [...values].sort((a, b) => a - b);
  return Array.from({ length: n - 1 }, (_, i) => sorted[Math.floor(((i + 1) / n) * sorted.length)]);
}

function bucketIndex(v: number, breaks: number[]): number {
  for (let i = 0; i < breaks.length; i++) if (v <= breaks[i]) return i;
  return breaks.length;
}

interface FolioRecord {
  folioId: string;
  family: string;
  rank: number;
  full: Set<string>;
  text: Set<string>;   // full minus label words
  label: Set<string>;  // label words only
}

interface LiftResult { within: number; between: number; lift: number; nWithin: number; nBetween: number; }

function computeLift(folios: FolioRecord[], vocabFn: (f: FolioRecord) => Set<string>): LiftResult {
  let withinSum = 0, withinN = 0, betweenSum = 0, betweenN = 0;
  for (let i = 0; i < folios.length; i++) {
    for (let j = i + 1; j < folios.length; j++) {
      const jac = jaccard(vocabFn(folios[i]), vocabFn(folios[j]));
      if (folios[i].family === folios[j].family) { withinSum += jac; withinN++; }
      else { betweenSum += jac; betweenN++; }
    }
  }
  const within  = withinN  > 0 ? withinSum  / withinN  : 0;
  const between = betweenN > 0 ? betweenSum / betweenN : 0;
  return { within, between, lift: between > 0 ? within / between : 0, nWithin: withinN, nBetween: betweenN };
}

async function main(): Promise<void> {
  console.log('jaccard-decomposition — label tier vs. text tier contribution to family signal');
  console.log(`Label words (${LABEL_WORDS.size}): ${[...LABEL_WORDS].join(', ')}`);
  console.log('');

  const [visionRows, evaRows] = await Promise.all([
    executeSql(`SELECT folio_id, subject_candidates FROM serverless_stable_qh44kx_catalog.voynich.folio_vision_analysis WHERE section = 'herbal' ORDER BY folio_id`),
    executeSql(`SELECT folio_id, eva_text FROM serverless_stable_qh44kx_catalog.voynich.eva_corpus WHERE section = 'herbal' ORDER BY folio_id`),
  ]);

  const evaMap = new Map<string, Set<string>>();
  for (const r of evaRows) {
    if (r.folio_id && r.eva_text) evaMap.set(r.folio_id, extractWords(r.eva_text));
  }

  const folios: FolioRecord[] = [];
  for (const r of visionRows) {
    if (!r.folio_id) continue;
    const full = evaMap.get(r.folio_id);
    if (!full || full.size < 10) continue;
    const family = classifyPlantFamily(r.subject_candidates ?? '[]');
    if (!BOTANICAL.has(family)) continue;
    const text  = new Set([...full].filter((w) => !LABEL_WORDS.has(w)));
    const label = new Set([...full].filter((w) => LABEL_WORDS.has(w)));
    folios.push({ folioId: r.folio_id, family, rank: folioToSortKey(r.folio_id), full, text, label });
  }

  folios.sort((a, b) => a.rank - b.rank);
  const rankIdx = new Map(folios.map((f, i) => [f.folioId, i]));
  console.log(`  ${folios.length} botanical herbal folios\n`);

  // -------------------------------------------------------------------------
  // Overall lift: FULL vs TEXT vs LABEL
  // -------------------------------------------------------------------------
  console.log('=== OVERALL JACCARD LIFT BY VOCABULARY CONDITION ===');
  console.log('');
  console.log('Condition'.padEnd(10) + 'Within'.padEnd(12) + 'Between'.padEnd(12) + 'Lift'.padEnd(10) + 'Pairs (W/B)');
  console.log('-'.repeat(56));

  for (const [label, fn] of [
    ['FULL',  (f: FolioRecord) => f.full],
    ['TEXT',  (f: FolioRecord) => f.text],
    ['LABEL', (f: FolioRecord) => f.label],
  ] as Array<[string, (f: FolioRecord) => Set<string>]>) {
    const r = computeLift(folios, fn);
    console.log(
      label.padEnd(10) +
      r.within.toFixed(4).padEnd(12) +
      r.between.toFixed(4).padEnd(12) +
      r.lift.toFixed(3).padEnd(10) +
      `${r.nWithin} / ${r.nBetween}`,
    );
  }
  console.log('');

  // -------------------------------------------------------------------------
  // Per-family-pair breakdown (FULL vs TEXT)
  // -------------------------------------------------------------------------
  const BIG3 = ['solanaceae', 'thistle', 'plantago'] as const;
  console.log('=== PER-FAMILY-PAIR JACCARD (FULL vs TEXT) ===');
  console.log('');
  console.log('Pair'.padEnd(24) + 'Full within'.padEnd(14) + 'Text within'.padEnd(14) + 'Full between'.padEnd(14) + 'Text between');
  console.log('-'.repeat(80));

  for (const famA of BIG3) {
    const aFolios = folios.filter((f) => f.family === famA);
    if (aFolios.length < 2) continue;

    // Within-family
    let fullWithinSum = 0, textWithinSum = 0, withinN = 0;
    for (let i = 0; i < aFolios.length; i++) {
      for (let j = i + 1; j < aFolios.length; j++) {
        fullWithinSum += jaccard(aFolios[i].full, aFolios[j].full);
        textWithinSum += jaccard(aFolios[i].text, aFolios[j].text);
        withinN++;
      }
    }
    const fullWithin = withinN > 0 ? fullWithinSum / withinN : 0;
    const textWithin = withinN > 0 ? textWithinSum / withinN : 0;

    // Between famA and each other big family
    for (const famB of BIG3) {
      if (famB <= famA) continue;
      const bFolios = folios.filter((f) => f.family === famB);
      let fullBetSum = 0, textBetSum = 0, betN = 0;
      for (const a of aFolios) {
        for (const b of bFolios) {
          fullBetSum += jaccard(a.full, b.full);
          textBetSum += jaccard(a.text, b.text);
          betN++;
        }
      }
      const fullBet = betN > 0 ? fullBetSum / betN : 0;
      const textBet = betN > 0 ? textBetSum / betN : 0;

      console.log(
        `${famA} vs ${famB}`.padEnd(24) +
        fullWithin.toFixed(4).padEnd(14) +
        textWithin.toFixed(4).padEnd(14) +
        fullBet.toFixed(4).padEnd(14) +
        textBet.toFixed(4),
      );
    }
  }
  console.log('');

  // -------------------------------------------------------------------------
  // Distance bins: FULL vs TEXT lift
  // -------------------------------------------------------------------------
  console.log('=== DISTANCE-BINNED LIFT: FULL vs TEXT (5 quantile bins) ===');
  console.log('');

  interface PairEntry { rankDist: number; fullJac: number; textJac: number; sameFamily: boolean; }
  const pairs: PairEntry[] = [];
  for (let i = 0; i < folios.length; i++) {
    for (let j = i + 1; j < folios.length; j++) {
      pairs.push({
        rankDist: Math.abs((rankIdx.get(folios[i].folioId) ?? i) - (rankIdx.get(folios[j].folioId) ?? j)),
        fullJac: jaccard(folios[i].full, folios[j].full),
        textJac: jaccard(folios[i].text, folios[j].text),
        sameFamily: folios[i].family === folios[j].family,
      });
    }
  }

  const breaks = quantileBreakpoints(pairs.map((p) => p.rankDist), 5);
  const bins: PairEntry[][] = Array.from({ length: 5 }, () => []);
  for (const p of pairs) bins[bucketIndex(p.rankDist, breaks)].push(p);

  const binLabels = [0, ...breaks].map((lo, i) => {
    const hi = breaks[i] ?? '∞';
    return `[${lo}–${hi}]`;
  });

  const mean = (arr: number[]) => arr.length ? arr.reduce((s, x) => s + x, 0) / arr.length : 0;

  console.log('Bin'.padEnd(14) + 'Pairs'.padEnd(8) +
    'Full W'.padEnd(10) + 'Text W'.padEnd(10) +
    'Full B'.padEnd(10) + 'Text B'.padEnd(10) +
    'Full lift'.padEnd(12) + 'Text lift');
  console.log('-'.repeat(86));

  for (let b = 0; b < 5; b++) {
    const bp = bins[b];
    const within  = bp.filter((p) => p.sameFamily);
    const between = bp.filter((p) => !p.sameFamily);
    const fw = mean(within.map((p) => p.fullJac));
    const tw = mean(within.map((p) => p.textJac));
    const fb = mean(between.map((p) => p.fullJac));
    const tb = mean(between.map((p) => p.textJac));
    const fLift = fb > 0 ? fw / fb : 0;
    const tLift = tb > 0 ? tw / tb : 0;
    console.log(
      binLabels[b].padEnd(14) +
      String(bp.length).padEnd(8) +
      fw.toFixed(4).padEnd(10) + tw.toFixed(4).padEnd(10) +
      fb.toFixed(4).padEnd(10) + tb.toFixed(4).padEnd(10) +
      fLift.toFixed(3).padEnd(12) + tLift.toFixed(3),
    );
  }

  console.log('');
  console.log('=== INTERPRETATION ===');
  const fullOverall  = computeLift(folios, (f) => f.full);
  const textOverall  = computeLift(folios, (f) => f.text);
  const labelOverall = computeLift(folios, (f) => f.label);
  const textRetained = fullOverall.lift > 1 ? (textOverall.lift - 1) / (fullOverall.lift - 1) : NaN;
  console.log('');
  console.log(`Full vocabulary lift:  ${fullOverall.lift.toFixed(3)}×`);
  console.log(`Text-only lift:        ${textOverall.lift.toFixed(3)}×  (label words removed)`);
  console.log(`Label-only lift:       ${labelOverall.lift.toFixed(3)}×  (only 13 label words)`);
  console.log(`Text tier retains:     ${(textRetained * 100).toFixed(1)}% of the family signal`);
  console.log('');
  if (textRetained >= 0.75) {
    console.log('VERDICT: Text tier is the primary carrier of the family signal.');
    console.log('  Removing 13 label words leaves ≥75% of the Jaccard lift intact.');
    console.log('  The two-tier structure has independent signals — label words mark folios,');
    console.log('  text words encode botanical content throughout the folio body.');
  } else if (textRetained >= 0.40) {
    console.log('VERDICT: Both tiers contribute to the family signal.');
    console.log(`  Text tier retains ${(textRetained * 100).toFixed(0)}% of lift after label removal.`);
    console.log('  Label words carry a meaningful share but are not the sole driver.');
  } else {
    console.log('VERDICT: Label words are the primary driver of the Jaccard family signal.');
    console.log(`  Text tier retains only ${(textRetained * 100).toFixed(0)}% of lift after label removal.`);
    console.log('  The family clustering is largely explained by the label tier.');
  }
}

main().catch((err) => {
  console.error('[jaccard-decomposition] fatal:', err);
  process.exit(1);
});
