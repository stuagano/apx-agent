# Phase 2: Grounding-Guided Mutation

**Date:** 2026-04-22
**Status:** Draft
**Depends on:** Phase 1 (vision grounding — complete)

## Problem

Phase 1 proved the grounding infrastructure works: seeds with botanical Latin text score grounding 0.5, while random decodings score 0. But random symbol-map mutation can never produce botanical text — the probability of randomly spelling "mandragora" via EVA character swaps is effectively zero. The grounding signal dies in one generation.

## Insight

The decipherer has two pieces of information it currently ignores:
1. **What the manuscript depicts** (available from `folio_vision_analysis`)
2. **What words would label those depictions** (expected terms per language)

If the decipherer knows the *target* plaintext for a folio, it can reverse-engineer a symbol map: "I need EVA sequence X to produce Latin word Y, so I need these specific character mappings." This is constraint satisfaction, not random search.

## Approach: Reverse-Engineering Mutation

Replace the current "swap two random symbols" mutation with a targeted approach:

1. **Pick a folio** — randomly select a herbal folio from the vision analysis
2. **Get expected terms** — look up what plant is depicted, what terms are expected in the candidate language
3. **Get EVA text for that folio** — look up actual EVA transcription from the corpus
4. **Reverse-engineer mappings** — given EVA word "daiin" and target word "mand", deduce d→m, a→a, i→n, n→d
5. **Merge with parent** — keep the parent's symbol_map but override the newly deduced mappings
6. **Set decoded_sample** — apply the merged map to the folio's EVA text so the grounder can score it

This preserves the evolutionary structure (parents, mutation, selection) but makes each mutation *informed* by the grounding target.

## New Tool: `reverse_engineer_mapping`

Added to the decipherer agent:

```ts
defineTool({
  name: 'reverse_engineer_mapping',
  parameters: z.object({
    eva_word: z.string(),      // e.g., "daiin"
    target_word: z.string(),   // e.g., "mandragora"
    parent_map: z.any(),       // existing symbol map to merge into
  }),
  handler: ({ eva_word, target_word, parent_map }) => {
    // Tokenize EVA word into characters (handling ch, sh, th digraphs)
    // Align with target word characters
    // Return merged symbol map
  },
})
```

## New Data Dependency: EVA Transcriptions per Folio

The decipherer needs actual EVA text for each herbal folio. This exists in the `voynich.corpus` table (loaded by notebook 01). The decipherer queries it at mutation time.

## Architecture Changes

### Decipherer: new tool + updated instructions

- Add `reverse_engineer_mapping` tool
- Add SQL helper to read EVA text from corpus table
- Add SQL helper to read expected terms from `folio_vision_analysis`
- Update instructions: "When mutating a parent, pick a random herbal folio, look up its EVA text and expected plant terms, then use reverse_engineer_mapping to create a child hypothesis whose symbol_map is biased toward producing those plant terms"

### Orchestrator: pass section context

- Add `section: "herbal"` to the mutation payload so the decipherer knows to use herbal folios

### Grounder: no changes

Already handles both `decoded_text` and `symbol_map` inputs.

### Population store: no changes

Already stores arbitrary metadata including `decoded_sample`.

## Expected Outcome

Children will have symbol maps that produce partial botanical terms when applied to herbal folio EVA text. Even imperfect mappings (e.g., "mand" instead of "mandragora") should score grounding > 0 via the stem-matching logic (0.4 for 4-char prefix match). Selection will then prefer children with higher grounding, compounding over generations.

## Deliverables

1. `reverse_engineer_mapping` tool in decipherer
2. SQL helpers in decipherer for reading EVA corpus + vision analysis
3. Updated decipherer instructions for grounding-guided mutation
4. Updated orchestrator mutation payload with section context
5. Test: verify children produce grounding > 0 within 3 generations
