# Voynich Parallel-Orchestrator Experiment Log

A record of what we tested, what we learned, and why we're moving on. Companion
to (not replacement for) the broader `FINDINGS.md` at the repo root.

## Goal

Empirically test whether the Voynich Manuscript's EVA transcription can be
decoded as a known Western/Semitic language under any simple cipher class,
using a parallel multi-agent orchestrator that runs each hypothesis as an
independent search.

## Architecture

| Component | Role |
| --- | --- |
| `voynich-orchestrator` (App) | Read-only dashboard + A2A surface. Reads UC tables only. Pure SSR. |
| `voynich-{cipher}-{lang}` (App ×5) | One loop-runner per hypothesis. Each is its own multi-agent orchestrator. |
| `voynich-decipherer` | Mutation agent (shared) |
| `voynich-historian` `voynich-critic` `voynich-grounder` | Fitness agents (shared) |
| `voynich-judge` | Verdict agent (shared) |
| `voynich.theories` (UC table) | Evidence board: every theory ever proposed. |
| `voynich.loop_logs` (UC table) | Structured log lines from all runner processes. |
| `voynich.strategy_stats` (UC table) | Per-strategy attempts + best score + exhausted flag. |

Each loop-runner reads `CIPHER_FOCUS` and `LANGUAGE_FOCUS` env vars and runs
`runTheoryLoop()` continuously. All five share the same downstream worker pool;
they coordinate only through the UC tables. Adding a new hypothesis = deploying
one more App with different env vars from the same source.

## Hypotheses Tested

Five live orchestrators × thousands of theories each. Two homophonic strategies
were stopped early when they stalled before the convergence work landed.

| Strategy | Mechanism | Peak (old scoring) | Peak (final scoring) | Status |
| --- | --- | ---:| ---:| --- |
| substitution + italian | 1:1 EVA glyph → Italian letter | 0.588 | **0.332** | converged |
| substitution + hebrew | 1:1 EVA → transliterated Hebrew letter | 0.489 | **0.249** | converged |
| positional + italian | 3-position 1:1 (initial/middle/final) | 0.610 | **0.406** | converged |
| positional + latin | 3-position 1:1 → Latin | 0.371 | **0.200** | converged |
| syllabic + italian | EVA word → 2-3 char Italian syllable | 0.847 | **0.363** | converged |
| homophonic + italian | many EVA tokens → 1 Italian letter | 0.422 | (stopped) | exhausted before final scoring |
| homophonic + latin | same → Latin | 0.482 | (stopped) | exhausted before final scoring |

"Peak" = max `grounding_score + consistency_score` ∈ [0, 2] under the relevant
scoring formula. The dramatic drop between old and final columns is the
operative finding (see *Scoring Evolution* below).

## Pathologies Discovered

The system kept finding "high-scoring" theories that were structurally
defective. Three distinct shapes:

### Pathology 1 — single-stem saturation (Italian, Hebrew)

```
"ibuco a bul iqil serva ilbrrvul iqilit utnervul frqrva serva fiqil ivul..."
```

SA found maps that produce one real dictionary word (`serva`, `hxlb`) six or
more times across the decoded text, padded with junk. Per-exact-word caps
weren't enough — `serva`/`servul`/`serril` are different exact words even
though they're clearly the same stem. Fix: stem-level dedup. Not enough.

### Pathology 2 — small-folio function-word dump (syllabic)

```
"lo del di ma se il con le la"
```

When a folio's EVA sample tokenises to ~9 unique words, the codebook can map
every one to a real Italian function word. 100% dict hit rate, 100% "real
words", LLM judge gave grounding=0.66 → combined 0.847. Fix: overfit penalty
capping grounding at 2× consistency. Helps but doesn't eliminate.

### Pathology 3 — uniform junk-with-stems (positional)

```
"qilvserva ilbita fiqilil iqulva iqrvil it ilitil fiqrrva fibnera fiqa lsera..."
```

After the dedup + penalty fixes, SA found maps that produced the SAME
`*serva`/`fib*`/`fiq*` cluster pattern on every folio. Cross-folio
consistency stays moderate (because every folio gets the same junk-with-stems
output), so the overfit penalty doesn't trigger. Effectively a uniformly-bad
solution that the structural scorers couldn't distinguish from a real one.
Fix: LLM coherence multiplier. This caught it.

### Bonus finding — apparent cross-cipher agreement was illusion

Substitution + Italian and positional + Italian both peaked at 0.588 and 0.610
respectively on the *same folio* (f77v). Initially looked like independent
ciphers agreeing — strong signal. On inspection, positional's 60-entry symbol
map had **identical mappings across all three position prefixes** (`f:`, `i:`,
`m:`). Positional SA had converged to a substitution-equivalent solution. Same
cipher, rediscovered with more freedom.

## Scoring Evolution

Six generations of scoring as each pathology was uncovered:

| Version | What changed | What it fixed | What it failed |
| --- | --- | --- | --- |
| 0 (initial) | `dict × quality + lm + termScore` | baseline | rewards "looks Italian shape" generally |
| 1 | per-exact-word cap | exact repeats | near-duplicates (`serva`/`servul`) |
| 2 | stem-level cap (3-char) | near-duplicates | repeated junk-clusters with different stems |
| 3 | `coherenceBonus` (consecutive real-word runs) | isolated dict hits | dense function-word strings |
| 4 | `bigramFluency` (corpus word-pair match) | function-word strings | uniformly-bad junk-with-stems |
| 5 | `applyOverfitPenalty` (grd ≤ 2 × cons) | folio-specific cherry-picking | uniform-pathology that fakes consistency |
| 6 | `llmCoherence` multiplier (Claude judges 0-1) | everything above | this is now the binding constraint |

**Key insight from this progression**: every patch we applied to the rule-based
scorer revealed the next loophole. The LLM coherence check broke the cycle
because it reads the text, not the statistics. After it landed, all five
runners converged within a day.

## Empirical Conclusions

1. **The simple-cipher hypothesis class is exhausted.** Across 5 cipher ×
   language combinations and ~50,000 theories, no map produces text the LLM
   judges as coherent prose. Peak coherence ratings sit around 0.3-0.4
   ("scattered real words inside nonsense").

2. **Language matters more than cipher (within the class).** Italian and Latin
   peak at similar levels regardless of cipher mechanism. Hebrew slightly
   lower, but with the same pathology shape. This suggests the bottleneck is
   linguistic structure, not the substitution geometry.

3. **The previous "high scores" (0.588, 0.610, 0.847) were scoring artifacts,
   not partial decodings.** They survived rule-based filters that rewarded
   pattern-matching over coherence. None of them produced text the LLM rated
   above 0.4 coherence.

4. **The convergence-detection-based architecture works.** Each runner reliably
   detected its own exhaustion and stopped at the right time. Stale-burst
   counters let the system declare a hypothesis dead without manual
   intervention.

5. **The multi-orchestrator pattern is the right abstraction.** Each app is an
   independent multi-agent loop testing one hypothesis. Adding a new
   hypothesis is one deploy. Killing a discredited one is `databricks apps
   stop`. The dashboard reads aggregated evidence without coupling to any
   runner.

## What's Still Worth Trying

The hypothesis class we exhausted: **single-pass cipher into a known dictionary
language**. Three classes outside it remain plausible and would slot cleanly
into the existing orchestrator:

### Polyalphabetic (Vigenère-style)

A repeating-key cipher where each glyph's substitution depends on its position
modulo a key length. Historically attested in the 15th-16th century — fits
the Voynich timeframe. Single-substitution cannot simulate it; if Voynich is
polyalphabetic, all of our prior tests were structurally incapable of
solving it.

Implementation: new `CipherType = 'polyalphabetic'` branch in `proposeTheory`,
SA over (key_length, key_substitution_table). Probably ~2-3 hours of work
since existing scaffolding (folio loading, agent calls, persistence) is
reusable.

### Compositional / morphological

Pelling, Bax, and others propose Voynich words are constructed
morphologically — prefix + root + suffix — and the cipher operates at the
morpheme level, not the glyph or word level. EVA tokens like `qok`, `che`,
`ee` would map to grammatical morphemes (article, noun stem, verb ending).

This is a different *unit* of decoding. Existing `syllabic` is closest but
treats whole EVA words as units; compositional would parse EVA words into
sub-units first, then translate each.

Implementation cost: higher (~6-8 hours). Need a morpho-parser for EVA
words plus a target-language morphology table.

### Strict-coherence scoring without a new cipher

Keep the cipher classes but make scoring *much* stricter — e.g. require
grounding > 0.5 to land in elite pool, require LLM coherence > 0.6 to
be considered "interesting." Run for longer with no churn from local
optima. This validates whether the existing classes have anything at all
to give, or if they're as exhausted as the data suggests.

Lower information value: we already have strong evidence the classes are
exhausted. Worth it only as a "confidence check" before committing major
effort to a new class.

## Recommendation

The architectural pattern (multi-orchestrator + UC evidence board + dashboard)
proved its value: we tested 5 hypotheses, found 3 pathologies, evolved scoring
through 6 generations, and reached a defensible negative conclusion in roughly
a week. Adding **polyalphabetic** would be the cheapest next experiment — it's
the simplest hypothesis class we haven't tested and is historically the most
plausible 15th-century cipher.

**Compositional** is more ambitious but has the highest upside if it works.
It's the only class that respects the *structure* of EVA words (prefix-root-
suffix patterns documented by linguistic analyses), and it's what serious
Voynich researchers have pursued in recent years.

Either fits the orchestrator pattern with no architectural changes — just a
new `CipherType` branch and a new app deploy.
