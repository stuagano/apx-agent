# Voynich Falsification Pipeline — Results & Framework

**Status:** Pivot complete (2026-04-30). The project's deliverable is the **falsification infrastructure**, not a decipherment.

---

## What was tested

| Run | Cipher family | Configurations | Rounds | PASS |
|-----|--------------|----------------|--------|------|
| Backtest (`19c59be`) | substitution, substitution-strip, verbose, positional, homophonic, polyalphabetic | cold + elite seeds; Latin + Italian | 991 | 0 |
| Substitution comparison (`cmp-1777568282885`) | substitution | N=1, N=5, single-char tokenization; pinned latin\|substitution\|cold | 60 | 0 |
| Transposition comparison (A1) | columnar transposition | N=1, N=5; K∈[4..8]; SA 1500 steps/K | 40 | 0 |
| **Total** | | | **1091** | **0** |

All rounds evaluated with: heuristic likelihood scorer (trigram LM + morphology + word-length) + dual null-hypothesis baselines (within-word shuffle, across-text shuffle) + LLM judge (gated on `likelihood ≥ 0.3` AND null distinguishable). The LLM judge correctly rejected every theory that cleared the heuristic gate — producing 304 FAIL verdicts and 0 PASS across the full dataset.

---

## Falsification claims (defensible)

1. **Substitution cipher of Latin (or Italian) is empirically dead** across 1051 rounds covering six cipher sub-families, two languages, cold and elite seeds, single-char and multi-glyph EVA tokenization, and N=1 to N=5 multi-restart search. The decoder produces Latin-shaped output the LLM judge correctly identifies as incoherent.

2. **Columnar transposition of Latin is empirically dead** across 40 rounds with K∈[4..8] and both N=1 and N=5 restarts. The SA hill-climb found no gradient — restart spreads were 0.001 (vs 0.02–0.08 for substitution), meaning the transposition search space has no exploitable structure on EVA data.

3. **Multi-restart (N=5) does not lift the PASS rate** under either cipher family. Bimodal calibration confirmed 13% per-trial success on *known Latin* (where a global optimum exists), projecting ~51% per-round. On EVA data, all restarts converge to the same local plateau — confirming there is no global optimum to find.

4. **Heuristic likelihood is a red herring.** avg_lik climbed monotonically across substitution phases (0.366 → 0.422 → 0.466) while PASS stayed at 0. The LLM judge is the load-bearing discriminator; heuristics alone overcount.

These results are consistent with the null hypothesis (Rugg cardan grille / Timm-Schinner): the Voynich manuscript is procedurally-generated nonsense, and no substitution- or transposition-family decoder will produce coherent text from it.

---

## What is NOT claimed

- Greek, Hebrew, Old Czech, or other language targets have not been tested under substitution.
- Fractionated ciphers (Polybius square), codebook (EVA token → whole word), or Vigenère with key recovery have not been tested.
- The null result does not rule out: (a) a cipher family outside those tested, (b) a real but extremely unusual language, or (c) a key-based cipher where key recovery is the bottleneck.

---

## The framework (reusable)

### Critic agent (`deploy/voynich-critic/`)

- **Composite likelihood scorer** (`scoring.ts`): trigram LM + morphological endings + word-length distribution. Calibrated against three classes: real Latin (0.660), scrambled Latin (0.404), random EVA-like (0.281). Margins ≥ 0.05. Per-language via `LANG_REFERENCES` record — add a new language by extending the record with bigram table, endings array, char set, and word-length bounds.
- **Dual null-hypothesis baselines**: within-word character shuffle (easy null) + across-text character redistribution (hard null). Both p-values must be < 0.1 for a theory to reach the LLM judge. N=25 shuffles per mode.
- **LLM judge**: strict PASS/FAIL prompt asking whether decoded text parses as grammatically coherent medieval Latin/Italian. Conservative: fragmentary, ambiguous, or "Latin-shaped-but-incoherent" → FAIL. Gated by heuristic threshold to control cost (one LLM call per judged theory).
- **Tool endpoints**: `find_contradictions`, `score_latin_likelihood`, `null_baseline_test`, `llm_judge` — all callable as direct POST endpoints, bypassing the LLM-tool-calling loop.

### Orchestrator (`deploy/voynich-orchestrator/`)

- **Calibration script** (`decoder-calibrate.ts`): verifies scorer ordering (real > scrambled > random) with ≥ 0.05 margins before any run.
- **Comparison harness** (`compare-restart-tokenization.ts`, `compare-transposition.ts`): pinned-strategy multi-phase batch with `batch_label` tags on the Delta table. Apples-to-apples comparison of any two decoder configurations. `strategyOverride?: Strategy` parameter on `runTheoryLoop` ensures all phases use the same cipher family.
- **Backtest script** (`backtest-critic.ts`): resumable (skips rows with existing `critic_likelihood`), runs historical theories through the critic via direct tool endpoints.
- **Cross-agent traces**: each critic tool call emits an `agent_call` span with `childUrl + childTraceId`, wiring orchestrator and critic into the same trace tree.
- **Per-language criticization**: `compositeLikelihood(text, language='latin')` dispatches to the right bigram/morphology reference. Orchestrator passes `sourceLanguage` through to the critic.
- **Multi-restart search** (`N_RESTARTS` env): per-round `Promise.all` over N independent SA restarts; best score wins. Quantified: 13% cold-start success on known Latin → ~51% per-round at N=5 (when an optimum exists).
- **EVA tokenization toggle** (`EVA_TOKENIZATION` env, read per-call): `multi-glyph` (production default) vs `single-char` (alternative hypothesis). Switching does not require redeployment.
- **`batch_label` column** on `voynich.theories`: idempotent `ALTER TABLE ADD COLUMN`, threaded through `runTheoryLoop`. Enables overlapping production runs without data corruption.

### Delta schema (`serverless_stable_qh44kx_catalog.voynich.theories`)

Five critic columns added idempotently: `skeptic_verdict`, `critic_likelihood`, `critic_adversarial`, `critic_null_distinguishable`, `critic_judge_verdict`. Plus `batch_label STRING` for experiment isolation.

---

## Run commands (for reference)

```bash
# Environment
export DATABRICKS_TOKEN=$(databricks auth token --profile fe-stable -o json | jq -r .access_token)
export DATABRICKS_HOST=https://fevm-serverless-stable-qh44kx.cloud.databricks.com
export DATABRICKS_WAREHOUSE_ID=76cf70399b8d0ef0
export CRITIC_AGENT_URL=https://voynich-critic-7474652869938903.aws.databricksapps.com

# Calibrate scorer before running
cd typescript && npx tsx examples/voynich/critic/calibrate.ts

# Run substitution comparison (60 rounds, ~20 min)
cd typescript/deploy/voynich-orchestrator && npx tsx compare-restart-tokenization.ts

# Run transposition comparison (40 rounds, ~13 min per phase)
npx tsx compare-transposition.ts

# Backtest historical theories through critic
npx tsx backtest-critic.ts

# If a phase run dies mid-way (token expiration):
npx tsx rerun-transposition-phase2.ts   # adapt the label constants for other phases
```

**Token lifetime warning:** Databricks OAuth tokens expire ~20–25 minutes. For runs longer than that, pre-refresh the token or split into per-phase scripts with fresh tokens at each start.

---

## Exploratory results (post-pivot, curiosity-driven)

### Word-level codebook decoder (`wordlevel-decoder.ts`, 2026-04-30)

Tested the hypothesis that each EVA word type maps to one Latin word (EVA word → Latin word codebook).

**Calibration failure**: SA cannot recover a known Latin word→code mapping (best 0.332 vs ground-truth 0.388, −14%). The char-trigram LM gradient is too weak at word level: 164 vocabulary items over 353 tokens provides ~2.15 repetitions per type — not enough for reliable hill-climbing.

**EVA phase**: best score 0.215 (below the 0.40 meaningful threshold), restart spread 0.013 (marginal, like transposition). The EVA corpus has **8,168 unique word types** from 36,290 tokens. A word→word codebook of Latin (max ~500 words) could only explain ~6% of the EVA type vocabulary. The type count alone falsifies a simple codebook.

**Verdict**: word-level codebook is not supported at any of three diagnostic levels — calibration, score threshold, or type-count feasibility.

### Abbreviation morphology null test (`abbreviation-analysis.ts` update, 2026-04-30)

Added a shuffled-EVA null: characters within each EVA word shuffled randomly (word boundaries preserved), same 7 metrics computed.

**Result**: **0 EVA-specific passes**. All 6 passing metrics (type/token ratio, word length, % 2-7 tokens, initial/terminal concentration, pos-last entropy) are also satisfied by the shuffled null. The "consistent with medieval Latin abbreviation norms" verdict from the prior run was an artifact of the word-length distribution — not of actual positional structure within EVA words.

The only metric that fails (hapax rate HIGH = 0.687 > 0.65) also fails for the null. EVA words have too many hapaxes relative to abbreviation text norms.

**Interpretation**: the abbreviation morphology metrics are not detecting real EVA structure. Any corpus of short words (2-5 chars) drawn from a small alphabet would pass these tests. The "consistent" finding was a false positive from tests that lack discriminative power.

---

## What is NOT claimed (updated)

- Greek, Hebrew, Old Czech, or other language targets have not been tested under substitution.
- Fractionated ciphers (Polybius square), Vigenère with key recovery have not been tested.
- The null result does not rule out: (a) a cipher family outside those tested, (b) a real but extremely unusual language, or (c) a key-based cipher where key recovery is the bottleneck.
- Abbreviation morphology analysis shows EVA is *consistent with* short-word corpora in general — not specifically with medieval Latin abbreviation conventions.
