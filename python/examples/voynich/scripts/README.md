# Jakobsen simulated annealing on Voynich sections

Standalone cryptanalysis scripts that test the **monoalphabetic substitution
hypothesis** for each section of the Voynich manuscript against three
plaintext-language candidates: Latin, Hebrew, and Arabic.

These run outside the agent framework — pure CPU computation, no Databricks
dependency. The TypeScript agent at
`typescript/examples/voynich/annealer/app.ts` exposes the same algorithm as a
callable tool so the orchestrator can short-circuit the EA loop when SA has
already rejected a hypothesis.

## Why Jakobsen, not the EA loop

Jakobsen's algorithm (1995) is the standard fast solver for monoalphabetic
substitution: propose a swap of two key entries, accept by the Metropolis
criterion, cool the temperature on a schedule. With a reasonable bigram model
it converges in **10-30k swaps (~5-10 seconds CPU)** on real ciphertext.

If multiple restarts converge to the same key, you've found the substitution.
If restarts diverge to wildly different keys with similar scores, *the cipher
isn't monoalphabetic* — and you can stop spending evolutionary generations on
that hypothesis branch.

## Files

- `eva_sections.py` — embedded EVA samples per Voynich section (herbal,
  astronomical, balneological, pharmaceutical, recipes). Lifted from the
  publicly-distributed Zandbergen-Landini transliteration. Tokeniser matches
  `notebooks/01_load_corpus.py`.
- `ngram_model.py` — character unigram + bigram log-probability tables for
  Latin, Hebrew (Latin transliteration), Arabic (Latin transliteration). No
  source corpora are embedded; only published statistics.
- `jakobsen_sa.py` — the SA solver. Includes IC and adjacent-glyph repeat-rate
  diagnostics, a multi-restart convergence test, and a verdict heuristic.
- `run_analysis.py` — runs section × language SA and writes `sa_results.json`.
- `sa_results.json` — output of the canonical run (committed for reference).

## Running

```bash
cd python/examples/voynich
python3 -m scripts.run_analysis
# or with explicit knobs:
python3 -m scripts.run_analysis --iterations 20000 --restarts 4 --seed 42
```

A full pass (5 sections × 3 languages × 4 restarts × 20k iterations) takes
about 90-120 seconds on a single core.

## What the canonical run found

Every section rejects monoalphabetic substitution against all three languages:

| section         | best language | per-token score | converged? | IC    | adj-repeat |
|-----------------|---------------|-----------------|------------|-------|------------|
| herbal          | latin         | -2.86           | no         | 0.076 | 0.075      |
| astronomical    | arabic        | -2.44           | mixed      | 0.127 | 0.054      |
| balneological   | latin         | -2.58           | no         | 0.111 | 0.097      |
| pharmaceutical  | latin         | -2.65           | no         | 0.097 | 0.076      |
| recipes         | arabic        | -2.42           | no         | 0.127 | 0.053      |

Two signals matter:

1. **Restarts diverge.** When SA finds a real substitution key, restarts from
   different seeds converge to the same key (or a permutation that produces
   identical decoded text). Here, every section's restarts disagreed — the
   bigram landscape has no single dominant basin.
2. **Adjacent-glyph repeat rate is 50-100x natural.** Plaintext Latin/Hebrew/
   Arabic sees adjacent identical characters at ~0.001 frequency. Voynich
   sections score 0.05-0.10. This is the well-known "qokeedy qokeedy ykeedy"
   pathology — *no* monoalphabetic key can produce that on natural-language
   plaintext.

The IC ≈ 0.08 figure that's often cited as evidence-for-substitution does
hold up here, but it's misleading: a peaked IC is necessary but not
sufficient. The repeat-rate test directly contradicts the monoalphabetic
hypothesis.

**Implication for the orchestrator**: drop `cipher_type = "substitution"`
from the active search space at least for these five sections. The remaining
candidates from `voynich-config.ts` — `polyalphabetic`, `null_bearing`,
`transposition`, `composite`, `steganographic` — are all consistent with the
observed repeat structure (Vigenère with short keys, verbose ciphers, and
prefix-based null padding all naturally produce adjacent-token similarity).

## Limitations

- The embedded EVA samples are abbreviated (~500-1500 tokens per section). The
  full corpus is in the `voynich_corpus.eva_words` Delta table; pointing the
  solver at that should sharpen scores but won't change the verdict — restart
  divergence is a structural property of the text, not a sample-size artifact.
- The bigram tables are smoothed at the unigram-product level for missing
  pairs. A trigram model trained on a real medieval Latin corpus would give
  cleaner per-token scores but, again, won't rescue a key that doesn't exist.
- Hebrew and Arabic are scored against Latin transliterations of their
  consonant inventories. If the plaintext language is actually one of these
  written in their native script, the consonant-only romanization is still a
  valid proxy — vowels would only add noise to the score, not signal.
