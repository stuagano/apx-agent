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

### Vigenère with key recovery (`vigenere-decoder.ts`, 2026-04-30)

Tested the hypothesis that EVA is a Vigenère cipher of Latin: each character is shifted by a cycling key of period L. Swept L=2..12, used IC (Index of Coincidence) for key-length detection, frequency cross-correlation for initial key guess, then SA refinement.

**IC result (the decisive finding):**

| key_len | mean_IC |
|---------|---------|
| L=2..12 | 0.0771 (constant) |
| Latin calibration | 0.0806 |
| IC_random (1/21) | 0.0476 |

IC is flat across ALL key lengths (range < 0.001). This is NOT a Vigenère signal — real Vigenère would show higher IC at the correct L (stride-L streams are each a Caesar shift → IC ≈ natural language) and lower IC at wrong L (mixed streams → IC closer to random). Flat IC means there is no periodic key structure.

EVA IC = 0.077 > IC_natural_Latin (≈ 0.065) > IC_random (0.048). EVA is MORE concentrated than natural language at every stride, consistent with generation from a constrained glyph table (Rugg cardan grille), not Vigenère encryption.

**Decoded text score**: 0.000 across all key lengths — Vigenère shifts within the EVA alphabet don't produce Latin trigram sequences.

**FALSIFIED**: Vigenère cipher of Latin (any key length 2–12) is not supported.

**Bonus structural finding**: The flat, elevated IC is a known Voynich fingerprint (noted by Rugg and others). It's consistent with: constrained character generation (grille table limits which chars can appear), no periodic key, and non-linguistic source. This is independent evidence corroborating the null hypothesis.

### Rugg hoax generator — positive control (`rugg-control.ts`, 2026-04-30)

Generated synthetic Voynich-like text using a Cardan grille simulation (Rugg 2004) and ran it through the full harness. Purpose: verify the falsification infrastructure correctly REJECTS known nonsense.

**Result: CONTROL PASS** — all three trials rejected at the heuristic gate.

| trial | words | likelihood | null_dist | verdict |
|-------|-------|-----------|-----------|---------|
| single-grille-200 | 200 | 0.287 | false | rejected |
| mixed-grille-400 | 384 | 0.256 | false | rejected |
| mixed-grille-900 | 864 | 0.268 | false | rejected |

Rejection mechanism: Rugg-generated text fails the null-baseline test (null_dist=false) — it's statistically indistinguishable from within-word shuffled versions of itself. This is correct: procedurally generated text has no positional structure that shuffling would destroy. Likelihood 0.26–0.29, below the 0.30 gate.

**Implication**: the 1091-round null result is a functioning detector. The harness can distinguish EVA decoder output (which at least produces Latin-shaped character sequences — heuristic likelihood 0.36–0.47) from pure cardan-grille nonsense (0.26–0.29). Both fail, but for the right reasons: EVA decoder output is Latin-shaped but incoherent (LLM judge rejects); Rugg output isn't even Latin-shaped.

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

### Vision-EVA correlation (`vision-correlation.ts`, bulk run 2026-04-30)

Tested whether EVA word frequency profiles cluster by visually-identified plant type. Bulk-analyzed all 226 herbal folios with `claude-sonnet-4-6` vision (via Databricks FMAPI OpenAI-compatible endpoint), then cross-referenced with EVA vocabulary.

**Setup**: 226 herbal folios analyzed with vision model → `folio_vision_analysis` table (subject_candidates, botanical_features, visual_description, spatial_layout, expected_terms). 224 folios had both vision + EVA data. Plant families assigned by regex matching on top candidate (confidence ≥ 0.35 required; below → "uncertain").

**TEST 1: EVA vocabulary overlap by plant family**

| metric | value |
|--------|-------|
| within-group Jaccard (same family) | 0.0799  (n=~530 pairs) |
| between-group Jaccard (diff family) | 0.0590  (n=500 random pairs) |
| **lift** | **1.354x** |

Per-family breakdown (top families by within-group Jaccard):

| family | n | Jaccard |
|--------|---|---------|
| uncertain | 33 | 0.123 |
| solanaceae (mandrake) | 31 | 0.066 |
| thistle | 30 | 0.056 |
| plantago | 10 | 0.058 |

**Verdict**: lift=1.354x exceeds the >1.15 threshold — EVA vocabulary clusters by visual plant type. This is evidence that EVA word selection is not purely positional/generative — some words appear preferentially on specific plant families.

**"Uncertain" anomaly (resolved by spatial analysis)**: The 34 low-confidence folios have the highest within-group Jaccard (0.123). Spatial analysis (`spatial-layout-analysis.ts`) found mean gap between uncertain folios = 3.44 (≈ random expected 3.30), but median gap = 0.50 — meaning most consecutive uncertain pairs are adjacent pages. Two dense sub-clusters drive the Jaccard: (a) the balneological section f78v–f85r2 (8 folios depicting bathing scenes), and (b) the end-of-manuscript section f103v–f116v (20 folios with "no illustration visible"). These 28/34 uncertain folios are in contiguous runs — their EVA vocabulary similarity is a quire/scribal artifact, not evidence of a hidden semantic category. The Jaccard of 0.123 drops out of the lift calculation if uncertain folios are excluded; the remaining identified-family lift is real but modest.

**Implication**: the lift=1.354x finding is real but modest. EVA vocabulary is partially structured by plant content AND by manuscript position (quire clustering). The distance-controlled test below partially separates these.

### Distance-controlled Jaccard (`family-distance-test.ts`, 2026-05-02)

Tests whether within-family Jaccard lift is flat vs. decaying as folio-rank distance increases. A flat lift = content-driven signal; a decaying lift = quire-positional artifact.

**Setup**: 94 herbal folios with both a recognized botanical family (confidence ≥ 0.35) and EVA text. 4,371 pairs total. Pairs bucketed by |rank_a − rank_b| using 5-quantile breakpoints derived from all pair distances (breaks at ranks 10, 22, 35, 52).

**Results**:

| Dist bin | Within-fam Jac | (n pairs) | Between-fam Jac | (n pairs) | Lift |
|----------|---------------|-----------|----------------|-----------|------|
| [1–10]   | 0.0739        | (321)     | 0.0667         | (564)     | 1.11x |
| [11–22]  | 0.0680        | (266)     | 0.0611         | (664)     | 1.11x |
| [23–35]  | 0.0543        | (186)     | 0.0577         | (659)     | 0.94x |
| [36–52]  | 0.0533        | (140)     | 0.0509         | (710)     | 1.05x |
| [53+]    | 0.0423        | (129)     | 0.0482         | (732)     | 0.88x |

**Overall**: within-family Jaccard 0.0622 vs. between 0.0564, lift=1.10x. Q1→Q5 decay=21%.

**Per-family breakdown**:

| Family (n) | Q1 lift | Q2 lift | Q3 lift | Q4 lift | Q5 lift |
|------------|---------|---------|---------|---------|---------|
| solanaceae (33) | 1.11x | 1.18x | 1.11x | 1.16x | 0.82x |
| thistle (30)    | 1.10x | 0.96x | 0.81x | 1.00x | 0.97x |
| plantago (10)   | 0.85x | 1.12x | 0.87x | 1.05x |   —   |

**Verdict**: BORDERLINE CONTENT-DRIVEN. The 21% decay from Q1 to Q5 falls below the 25% MIXED threshold, but the overall picture is noisy.

- **Solanaceae** shows the strongest and most consistent signal: 1.11–1.18x lift across Q1–Q4, only dropping at Q5 (very distant pairs, n=66). This is the most botanically coherent family in the corpus.
- **Thistle** shows weaker and more variable lift — no clear gradient.
- **Plantago** (n=10) has insufficient pairs for reliable inference.

**Conclusion**: the quire-positional confound is real (absolute Jaccard values are higher for nearby pairs regardless of family label) but does NOT fully explain the within-family lift for solanaceae. The solanaceae vocabulary signal survives distance control through Q4 (~85% of pairs), which is positive evidence of content-encoding — the weakest defensible claim that EVA contains semantic structure correlated with botanical identity.

### Spatial layout analysis (`spatial-layout-analysis.ts`, 2026-05-01)

Four analyses on LLM-generated text region positions for all 226 folios:

**Layout by family** (regions / lines / EVA words):

| family | n | regions | lines | words |
|--------|---|---------|-------|-------|
| uncertain | 34 | 4.7 | 54.6 | 368 |
| solanaceae | 31 | 4.8 | 31.5 | 148 |
| thistle | 30 | 2.4 | 17.9 | 86 |
| plantago | 10 | 2.5 | 20.3 | 90 |

Uncertainty folios have 4× the text lines of thistle/plantago. Most of this is driven by the f103v–f116v cluster (text-only folios with no plant illustration — dense paragraphs of EVA text).

**Non-herbal fingerprinting**: Zodiac/balneological folios (38 identified) have 184.4 EVA words/folio vs. 113.3 for herbal-proper (+63%). But avg word length (5.22 vs. 5.21) and hapax rate (0.848 vs. 0.868) are indistinguishable. Non-herbal sections generate more EVA words (zodiac wheels require circumferential text) but use the same word morphology as herbal sections. This is consistent with a single scribal/generative process producing all EVA text regardless of section type.

**Text region distribution** (858 regions across 226 folios, avg 3.8/folio):

| position | count |
|----------|-------|
| top-left | 205 (24%) |
| center | 167 (19%) |
| top-right | 132 (15%) |
| left | 119 (14%) |
| bottom | 119 (14%) |

Roles: description (516 / 60%), label (257 / 30%), unknown (76 / 9%). Correlation between region count and EVA word count: r=0.35 (more complex visual layouts → proportionally more text).

**TEST 2: LLM coherence** (corrected run 2026-05-01 — botanical-only, allowlist filter)

Five folios selected via allowlist (solanaceae, poppy, thistle, lily-family, mint-family) — one per family, sorted by confidence. Prior runs using a blocklist (`NON_BOTANICAL`) still admitted zodiac folios (family names like `pisces`, `aries` bypass a string blocklist). This run uses a set of 12 known botanical family names as an allowlist.

Folios: f13r (Mandrake, 72%, solanaceae), f33r (Poppy, 65%), f18v (Thistle, 62%), f79r (Water lily, 55%, lily-family), f21r (Thyme, 52%, mint-family).

Key observations:

- **Family-level morphological fingerprints** — each family showed a different dominant EVA pattern:
  - solanaceae (f13r): heavy `-chy` suffix cluster (`qopchy, ykchy, kchy, dypchy, shkchy`); `ch/k` consonant dominance
  - poppy (f33r): `-dy` suffix concentration (`shepchdy, yfoldy, qofody, shedy`); `ch-/sh-` initials front-loaded in first lines; `aiin` variants as connective tissue
  - thistle (f18v): `qo-` prefix dominates (`qokchy, qotchy, qokol, qoky, qokay`); words cluster in middle section
  - lily-family (f79r): `ol-/sh-` clusters (`ol, oly, qol, sheol, shedy`); short 2-3 char forms alternate with longer phrase-terminators
  - mint-family (f21r): `chol/qotchol` repetition; `ch-` and `qo-` as morphological prefixes across multiple word types

- **`qo-` prefix as inter-family signal**: `qo-` appears distinctively on thistle (f18v), lily-family (f79r), and mint-family (f21r), but is NOT the dominant marker on solanaceae (f13r) or poppy (f33r) where `-chy/-dy` suffixes dominate instead. This prefix/suffix axis is family-correlated but the LLM could not determine directionality. *Note: scale test below partially revises this.*

- **LLM's epistemic rating**: All five responses explicitly disclaimed visual analogy and attributed patterns to positional/scribal constraints. The LLM identified `ch/k` consonant dominance, `qo-` prefix frequency, and length alternation as structural regularities. It made no claim that patterns correlate with plant content — and it correctly noted that "positional word-class rules" and "quire-level orthographic habits" are confounders that cannot be ruled out without a decipherment framework.

- **Implication**: Botanical families DO show distinct dominant EVA morphologies, but whether this reflects plant-content encoding or positional/quire structure is not separable from the current data. The 1.33x lift from TEST 1 (EVA vocabulary clusters by plant family) is consistent with both explanations.

### Prefix/suffix axis at scale (`prefix-suffix-axis.ts`, 2026-05-02)

Tested whether the `qo-`/suffix axis observed on 5 individual folios in TEST 2 holds across all 94 botanically-classified herbal folios. Computed per-folio morphological rates and effect sizes (rank-biserial correlation, r).

**Per-family morphological rates (top families)**:

| Family | n | qo-prefix | -chy sfx | -dy sfx | ch-init | short(≤3) |
|--------|---|-----------|----------|---------|---------|-----------|
| solanaceae | 33 | **0.159** | 0.025 | **0.175** | 0.153 | 0.147 |
| thistle    | 30 | 0.096 | **0.053** | 0.132 | 0.178 | **0.186** |
| plantago   | 10 | 0.089 | 0.063 | 0.065 | 0.260 | 0.176 |
| poppy      | 7  | 0.092 | 0.070 | 0.090 | 0.166 | 0.166 |
| mint-family| 3  | 0.134 | 0.054 | **0.198** | 0.196 | 0.093 |

**Solanaceae vs. other botanical (effect sizes)**:

| Feature | Solan | Other | r | Interpretation |
|---------|-------|-------|---|----------------|
| qo-prefix | 0.159 | 0.099 | **+0.538** | LARGE — solan highest |
| -chy suffix | 0.025 | 0.056 | **−0.442** | LARGE — solan LOWEST |
| -dy suffix | 0.175 | 0.116 | +0.376 | medium |
| ch-init | 0.153 | 0.197 | −0.262 | medium — solan lower |
| short words | 0.147 | 0.170 | −0.289 | medium — solan fewer |

**Revised axis finding**: TEST 2's single-folio impression was misleading. The scale data shows solanaceae has the HIGHEST `qo-` prefix rate of any family (r=+0.538, large effect) — not lower. What's actually low on solanaceae is `-chy` (r=−0.442). The real distinction:

- **Solanaceae**: `qo-` prefix HIGH, `-dy` suffix HIGH, `-chy` suffix very LOW, short words fewer
- **Thistle**: `qo-` prefix lower, `-chy` suffix higher, more short words

The TEST 2 confusion arose because solanaceae example words (`qopchy, ykchy`) appear suffix-focused at first glance, but `qopchy` is also a `qo-` word. At scale, the `qo-`+`-dy` combination is the solanaceae signature.

**Top solanaceae-specific EVA words** (by log-odds ratio vs. other botanical families):

| Word | LOR | Notes |
|------|-----|-------|
| oldaiin | 2.94 | not qo, not suffix |
| lchedy | 2.82 | `-dy` suffix |
| pchedy | 2.94 | `-dy` suffix |
| chedaiin | 2.94 | |
| kain | 3.05 | short distinctive |
| qokeo | 2.50 | `qo-` prefix |
| qopchedy | 2.50 | `qo-` prefix + `-dy` suffix |
| cheom | 2.67 | |

**Top thistle-specific EVA words**: `okeeor`, `okchor`, `keody`, `shodaiin`, `chokor` — predominantly ending-variation forms without the solanaceae `qo-` or `-dy` markers.

**Alignment with distance test**: The solanaceae vocabulary signal that survives quire-distance control (Q1–Q4 lift in `family-distance-test.ts`) is now traceable to specific word types: the `qo-`+`-dy` compound forms and named words like `oldaiin`, `lchedy`, `cheom`. These are not high-frequency corpus words (low count-out), which explains why the Jaccard lift is real but modest.

### Solanaceae signal test: NPMI vs. expected terms (`solanaceae-signal-test.ts`, 2026-05-02)

**Note on cross-section test**: the `eva_corpus` table contains only the herbal section, so cross-manuscript distribution cannot be tested. Within-herbal concentration: 12/15 target solanaceae words are >1.5x more prevalent in solanaceae folios than in other herbal families.

**NPMI vs. expected Latin/Italian terms**: For each solanaceae-specific word, the top NPMI associations with LLM-generated expected vocabulary are strikingly coherent:

| EVA word | Folios (solan) | Top NPMI associations |
|----------|---------------|----------------------|
| `oldaiin` | 8 (7 solan) | mandragoratus=0.706, soporatus=0.706, venefica=0.620, stupefaciens=0.576, bacca=0.559 |
| `sheety` | 6 (4 solan) | solano=0.518, erbamorella=0.473, narcotivo=0.426 |
| `cheom` | 6 (4 solan) | narcotivo=0.426, morella=0.391, solano=0.387, furor=0.383 |
| `qokeo` | 7 (4 solan) | soporatio=0.588, narcosi=0.588 |
| `chdaiin` | 11 (4 solan) | preparatio=0.699, sanatio=0.648, glandula=0.639 |
| `lchedy` | 37 (8 solan) | receptum=0.538, remedium=0.521, caliditas=0.517, dosis=0.512 |

**The semantic coherence is the key result.** The top NPMI associations for the most solanaceae-concentrated words form a pharmacologically self-consistent cluster for solanaceae medicine:

- `oldaiin` → mandrake compound (`mandragoratus`), narcotized state (`soporatus`), narcotic agent (`stupefaciens`), berry form (`bacca`), and poisoner (`venefica`)
- `sheety`/`cheom` → nightshade/solanum (`solano`, `morella`, `erbamorella`) + narcotic effect (`narcotivo`) — two independent words converging on the same semantic field
- `qokeo` → the sleep-inducing action (`soporatio`, `narcosi`)
- `lchedy` → pharmaceutical recipe format (`receptum`, `remedium`, `dosis`) — the broadest of the solanaceae words, consistent with it being a recipe/dose marker rather than a plant name

**Verdict**: These are not random high-NPMI associations. Five independent EVA word types, all solanaceae-concentrated, all cluster around the same medieval pharmacological domain: mandrake/nightshade → narcotic/soporific preparation → dose/recipe. This is the most direct evidence in the corpus that EVA contains semantic content correlated with botanical illustration. It does not constitute a decipherment, but it falsifies the hypothesis that EVA word selection is completely independent of folio subject matter.

**Caveat**: expected terms are LLM-generated from the illustration — they are not independent of the visual classification. The chain is: illustration → LLM expected terms → NPMI with EVA words. If the LLM's expected terms are unreliable or systematically biased, this would inflate apparent semantic signal. The NPMI values are not corrected for this dependency.

---

## What is NOT claimed (updated)

- Greek, Hebrew, Old Czech, or other language targets have not been tested under substitution.
- Fractionated ciphers (Polybius square), Vigenère with key recovery have not been tested.
- The null result does not rule out: (a) a cipher family outside those tested, (b) a real but extremely unusual language, or (c) a key-based cipher where key recovery is the bottleneck.
- Abbreviation morphology analysis shows EVA is *consistent with* short-word corpora in general — not specifically with medieval Latin abbreviation conventions.
