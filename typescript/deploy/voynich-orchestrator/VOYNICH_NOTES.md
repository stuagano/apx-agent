# Voynich Decipherment — Approaches Tracker

Working log of what we've tried, what we've learned, and what's still on the table.
The ground truth for current run state is `strategy_stats` and `theories` Delta tables;
this doc is for context that doesn't fit there.

Last updated: 2026-04-29

---

## Current best scores (image-grounded LM scorer)

Folios: `f1r-f20v` herbal section, 17 high-confidence with distinct EVA. All Currier Hand A.
Scoring: `term_overlap × 0.3 + (dict × quality) × 0.4 + LM × 0.3`, with image-derived
expected_terms (~50 Latin + ~50 Italian per folio) backfilled from LLM vision analysis.

| Strategy | Best | Status |
|---|---|---|
| latin\|substitution\|elite | 0.371 | exhausted |
| italian\|substitution-strip\|cold | 0.369 | progressing |
| italian\|substitution\|cold | 0.357 | progressing |
| latin\|substitution\|cold | 0.348 | exhausted |
| italian\|substitution-strip\|elite | 0.340 | exhausted |
| italian\|substitution\|elite | 0.338 | exhausted |
| latin\|substitution-strip\|elite | 0.328 | progressing |
| italian\|positional\|cold | 0.319 | progressing |
| latin\|substitution-strip\|cold | 0.319 | exhausted |
| italian\|positional\|elite | 0.303 | progressing |
| latin\|polyalphabetic\|elite | 0.286 | progressing |
| italian\|polyalphabetic\|cold | 0.281 | progressing |
| latin\|positional\|cold | 0.274 | exhausted |
| italian\|verbose\|cold | 0.269 | progressing |
| latin\|positional\|elite | 0.251 | progressing |
| latin\|verbose\|cold | 0.236 | exhausted |
| italian\|verbose\|elite | 0.232 | progressing |
| latin\|verbose\|elite | 0.228 | exhausted |

**Plateau:** ~0.37. Substitution maxes here; verbose/positional underperform.

**Qualitative shift since image-grounded scoring:** decoded outputs now contain real
content words matching the depicted plant. Examples seen on real bursts:
- f5r (geranium-like): `"...e vino acqrq eapu... la erb..."` — `vino` ×3, `erb` ×2, `la`
- f5r (latin): `"...em flos arxix... preia flos caflos..."` — `flos` ×4, `et`, `ad`, `ut`
- f18v: `"...erbq etla erba erla..."` — `erba` in multiple variants
- f13r: `"...lmno cuno vino ut..."` — `vino`, `una`, `uno`

This is new — under the old bigram heuristic, decoded text was statistical-noise
gibberish. Under image-grounded scoring with rich expected_terms, real Latin/Italian
words emerge from the cipher search.

---

## Cipher families tried

### substitution (1:1)
- Hill-climbing over EVA→Latin/Italian char map. Glyph frequency seed + crossbreeding +
  reverse-freq + vowel-hyp + phonetic + perturbed seeds.
- Plateau: 0.37 (best of any family).
- Verdict: best candidate so far but doesn't break through. Decoded output forms
  fragments of real words but never sustained Latin/Italian text.

### substitution-strip (Reeds null preprocessor)
- Strip word-initial `q` and word-final `y` before substitution. Hypothesis: those two
  glyphs are scribal markers/filler, not content.
- Plateau: 0.37 (essentially identical to plain substitution).
- Verdict: null-stripping doesn't help. Either q/y aren't nulls, or they are but
  the underlying text still isn't 1:1 substitution.

### verbose (many-to-many with simulated annealing)
- Maps EVA glyphs and digraphs (qok, dy, ee, ar, chedy…) to multi-char outputs, plus
  scribal-abbreviation candidates (y→us, dy→tio, aiin→ium…). 8K SA steps with
  cooling temperature. Elite mode overlays single-char mappings from substitution
  elite pool onto fresh verbose seed.
- Plateau: 0.27 (worse than substitution).
- Verdict: more parameters didn't unlock anything. Search space is much larger
  but the additional freedom doesn't help — the underlying signal isn't there.

### polyalphabetic (keyword-shifted substitution)
- Caesar-shift the substitution map per folio using a herbal keyword (HERBA, FLORA,
  RADIX, etc.).
- Plateau: 0.29.
- Verdict: didn't break anything, didn't help much. Limited keyword space tested.

### positional (3 sub-maps: word-initial/middle/final)
- Voynich glyphs cluster strongly by word position (q/k/t/p/ch initial,
  y/n/m/r/l final). Each position gets its own 1:1 substitution alphabet.
  Elite mode seeds all three buckets with a substitution elite, then SA breaks
  the symmetry.
- Plateau: 0.32.
- Verdict: position-conditional encoding doesn't help. Voynich's positional
  glyph clustering appears to be structural artifact, not extra encoding signal.

---

## Scoring function evolution

### Old — bigram-overlap heuristic (replaced)
- Score = fraction of bigrams in decoded text that appear in a hand-curated list
  of ~25 common Latin/Italian bigrams.
- Old "ceiling": 0.683 — but this was a function-word trap. Top maps just saturated
  with `est`, `ita`, `ad`, `post`. Decoded text wasn't real Latin, just a few
  function words repeated.

### Function-word saturation defense
- `dictionaryQuality(text, language)` multiplier penalizes when >50% of dictionary
  hits come from a small set of function words (`est`, `et`, `in`, `il`, `la`, etc.).
  Rewards vocabulary diversity.
- Effect: collapsed the 0.683 fake peak to its real value (~0.27).

### Character n-gram language model (current)
- Trigram model trained on inline medieval-herbal Latin and Italian corpora
  (~2200 trigrams, 700+ unique per language). Laplace-smoothed log-probability,
  linearly mapped to [0, 1] using empirical reference points.
- Replaces bigram heuristic. Real Latin scores ~0.7, gibberish ~0.2.
- Effect: dropped the substitution ceiling from 0.37 (bigram) to 0.31 (LM-only).

### Image-grounded expected_terms (current)
- LLM-backfilled `expected_terms` field in `folio_vision_analysis`: ~50 medieval
  Latin and ~50 Italian botanical/anatomical/descriptive terms per folio, derived
  from the LLM's `visual_description` of the actual image.
- `scoreTermOverlap` uses saturating denominator (5 strong matches = full score)
  rather than `terms.length` — was severely penalizing rich term lists before.
- Effect: substitution ceiling rose from 0.31 (LM only) to 0.37 (LM + image grounding).
  More importantly: **decoded text now contains real botanical content words**.

---

## Failed assumptions and discoveries

| Assumption | Reality | How we found out |
|---|---|---|
| Bigram-frequency match = good cipher | False — gameable by function-word saturation | The 0.683 trap |
| Function-word saturation = win | False — penalized as low-quality | dictionaryQuality multiplier |
| Null-stripping breaks plateau | False — same ceiling | substitution-strip results |
| Verbose extension breaks plateau | False — worse, larger search space | verbose SA results |
| Position-conditional encoding | False — same ceiling | positional cipher results |
| Currier A/B separation will help | False for our folio set | All f1-f20 are Hand A |
| Thin term list (5-10/folio) is enough | False — buried under noise | Backfill produced step change |

---

## In flight

### Homophonic cipher (Naibbe-style) — added 2026-04-29
Following Greshko et al. (Cryptologia 2025): verbose homophonic substitution
where each plaintext letter encodes to **multiple** distinct multi-glyph
strings. We invert this: many EVA tokens collapse to one plaintext letter.

This is the inverse cardinality of our verbose cipher. It directly addresses
the Karl K critique — under 1:1 substitution, all of Voynich's structural
weirdness must hide in the plaintext, producing gibberish. Homophonic gives
the cipher *somewhere else* to put the variety.

Implementation: curated ~50-token EVA homophone alphabet (singles, common
digraphs/trigraphs/4-grams from the literature: `qok`, `dy`, `chedy`, `aiin`,
`qokeedy`, etc.). SA over (token → letter) assignments, 8000 steps,
frequency-biased letter pool. Elite mode inherits single-char mappings from
the substitution elite pool. Strategies 17-20 of the rotation.

Now deployed. First homophonic burst hits ~75 minutes from deploy after
substitution rebuilds the elite pool.

## Outstanding hypotheses (not yet tested)

### A. Transposition / anagram (highest priority among untried)
Glyph order within each Voynich word is permuted before encoding. The cipher
search has been assuming positional order is preserved; if it isn't, no
substitution-family search will work no matter how clever. Karl K's comment in
Pelling's anti-Rugg post argues this directly: a mono-alphabetic cipher
"doesn't really transform the underlying plaintext" so the weirdness has to
go somewhere — transposition is one place.

Implementation idea: for each Voynich word, generate all permutations (or random
sample), apply the substitution map to each, score the best. Or: pre-permute the
EVA text before substitution search. SA over permutations.

### B. Non-Latin/Italian language priors
Czech (Bohemian provenance), Hebrew transliterated, Old Spanish, Old Occitan.
Cheap to test — swap the corpus and dictionary. If decoded scores spike under
one of these, that's the strongest signal we'd ever get. Bax's natural-language
hypothesis (controversial) gestures at this.

Implementation: add language to LANG_FREQ, LATIN_DICT-equivalent, the trigram
corpus. Run a batch with the new language as a strategy.

### C. Multi-agent loop (architecture's original intent)
Wire orchestrator to actually call deployed agents during search:
- voynich-grounder → image grounding (already deployed, currently bypassed)
- voynich-historian → historical/provenance grounding
- voynich-critic → adversarial review
- voynich-judge → final verdict on top theories
Heavy (LLM call per round); likely best applied to top candidates rather than
every round in inner loop.

### D. Expanded folio set + Currier A/B separation
Add pharmaceutical (f87-f93) and biological (f75-f84) folios — these are
predominantly Hand B. If a single cipher works on Hand A but fails on Hand B
(or vice versa), that's structural evidence for two distinct keys.

### E. Word-internal structure (Stolfi grammar)
Voynich words have rigid prefix-stem-suffix structure with strong glyph-position
constraints. Treat each word as `[prefix][stem][suffix]` and search separate
substitution maps for each part. Distinct from positional cipher (which
operates on chars, not word-parts).

### F. Hoax verification (Rugg-style table-and-grille generation)
Run our own table-based generator, see if the output produces a similar plateau
under our scorer. Not a decipherment but a control: if generated nonsense
hits ~0.37 too, that's evidence the plateau is intrinsic to "Voynich-shaped"
text regardless of meaning.

---

## Open questions

1. **Is 0.37 the cipher's ceiling or the text's?** If we ran our pipeline on a
   real medieval herbal text (Hortus Sanitatis, etc.) under the same scorer,
   what would it score? That calibration would tell us whether 0.37 means
   "decoded gibberish" or "weakly decoded real text."

2. **Do image-term hits stack?** Best-case f5r got `vino` ×3 and `erb` ×2 —
   that's 5 hits on one folio. Has any single map ever hit 5+ DIFFERENT image
   terms (not just repetitions of one) on the same folio? That would be much
   stronger signal than the aggregate score suggests.

3. **Does cross-folio consistency stack?** A real cipher should produce
   coherent text across multiple folios with the SAME map. Our consistency
   scores are typically 0.10-0.15, never high. Is that because the cipher
   isn't substitution, or because the dictionary is missing terms?

4. **Are we biased by Latin/Italian dict contents?** The dict is ~500 medieval
   Latin words. Voynich could be a real medieval medical text in a vocabulary
   we don't have. Worth: pull a wider word list (Liber de Simplici Medicina,
   Macer Floridus, etc.) and rerun.

5. **What does the LLM actually see in the high-scoring decoded outputs?**
   We've never asked Claude to read a top decoded fragment and say "is this
   coherent Latin/Italian, partial, or not." The skeptic prompt does
   something similar but adversarially. A separate "linguist" prompt could
   give a less-biased read.

---

## Architecture (for memory after context resets)

- **Orchestrator** (`voynich-orchestrator`, deployed Databricks App): runs the
  18-strategy × 20-round burst rotation. Self-contained — does not call other
  agents in the cipher inner loop. Reads `folio_vision_analysis` for image
  metadata, persists `theories` and `strategy_stats` to Delta.
- **Other deployed agents** (decipherer, historian, critic, grounder, judge):
  exist, deployed, exposed via A2A/MCP. Currently NOT called by orchestrator
  during cipher search. Originally designed for evolutionary loop; pivoted to
  in-process for performance.
- **Vision analysis cache** (`folio_vision_analysis` Delta table): pre-computed
  LLM image descriptions per folio. Now includes `expected_terms` (Latin +
  Italian botanical term lists, ~50 each per folio) backfilled by
  `backfill-expected-terms.ts`.
- **Profile**: `fe-stable`. Workspace:
  `https://fevm-serverless-stable-qh44kx.cloud.databricks.com`. Catalog:
  `serverless_stable_qh44kx_catalog.voynich.*`.

## Reset commands (for clean re-runs after scorer changes)

```sql
TRUNCATE TABLE serverless_stable_qh44kx_catalog.voynich.strategy_stats;
TRUNCATE TABLE serverless_stable_qh44kx_catalog.voynich.theories;
```

```bash
databricks workspace import-dir typescript/deploy/voynich-orchestrator \
  /Workspace/Users/stuart.gano@databricks.com/voynich-orchestrator \
  --overwrite --profile fe-stable
databricks apps deploy voynich-orchestrator \
  --source-code-path /Workspace/Users/stuart.gano@databricks.com/voynich-orchestrator \
  --profile fe-stable
```
