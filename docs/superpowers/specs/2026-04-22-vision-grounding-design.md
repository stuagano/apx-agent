# Vision Grounding System for Voynich Evolutionary Loop

**Date:** 2026-04-22
**Status:** Approved
**Scope:** Phase 1 — Herbal section only (~130 folios)

## Problem

The evolutionary loop has been running for 135+ generations with zero learning. Three of five fitness signals (perplexity, consistency, statistical) are LLM-hallucinated noise. The one real signal (semantic) is hard-capped at 0.70. The loop is a random sampler, not an evolutionary algorithm.

The Voynich Manuscript contains ~130 illustrated plant folios in the herbal section, each with text adjacent to the illustration. These images are free supervision — a correct decoding of text near a mandrake illustration should produce words related to mandrake. No prior approach has exploited this multimodal signal.

## Approach: Hybrid (Cached Analysis + Lightweight Scoring)

Two phases:

- **Phase 1a (one-time batch):** Download herbal folio images, run vision analysis via FMAPI, cache structured results to a Delta table.
- **Phase 1b (scoring agent):** New `voynich-grounder` Databricks App scores hypotheses against cached vision analysis. No vision calls in the hot loop. Optional escalation to live vision for high-scoring hypotheses.

## Architecture

### Data Flow

```
Phase 1a (batch, one-time):
  Beinecke IIIF → UC Volume (voynich/images/herbal/) → FMAPI Vision → folio_vision_analysis table

Phase 1b (per-generation scoring):
  Orchestrator → voynich-grounder agent → reads folio_vision_analysis → text overlap scoring → returns grounding score
```

### Agent Constellation

The grounder joins the existing agents as a new fitness agent:

| Agent | Role | Vision calls? |
|-------|------|--------------|
| Decipherer | Mutation | No |
| Historian | Semantic fitness | No |
| Critic | Adversarial fitness | No |
| **Grounder** | **Image grounding** | **No (reads cache)** |
| Judge | Adversarial review | No |

The orchestrator adds the grounder URL to `FITNESS_AGENT_URLS`. No changes to the evolutionary loop mechanics.

## Data Model

### New table: `folio_vision_analysis`

```sql
CREATE TABLE voynich.folio_vision_analysis (
  folio_id            STRING,       -- e.g., "f1r", "f2v"
  section             STRING,       -- "herbal" (all Phase 1)
  image_url           STRING,       -- IIIF source URL
  volume_path         STRING,       -- UC Volume path to downloaded image

  -- Vision model output
  subject_candidates  STRING,       -- JSON array: [{"name":"mandrake","confidence":0.8,"latin":"Mandragora","reasoning":"thick bifurcated root"}, ...]
  spatial_layout      STRING,       -- JSON: {"text_regions":[{"position":"top-right","role":"label","estimated_lines":2}, ...]}
  visual_description  STRING,       -- Free-text description from vision model
  botanical_features  STRING,       -- JSON array: ["serrated leaves", "thick taproot", "red berries"]

  -- Expected terms (lazily populated per candidate language, cached for reuse)
  expected_terms      STRING,       -- JSON: {"latin":["mandragora","radix",...], "italian":["mandragola",...]}

  -- Metadata
  model_id            STRING,       -- "databricks-claude-sonnet-4-6"
  analyzed_at         TIMESTAMP,
  prompt_version      STRING        -- version tag for re-run tracking
);
```

Key design decisions:
- `subject_candidates` keeps 1-3 candidates with confidence scores (medieval plant drawings are often ambiguous).
- `spatial_layout` maps text regions to roles (label vs description vs marginalia) so the grounder knows which decoded text to score against plant-name expectations.
- `expected_terms` starts empty, lazily populated per language on first request, then cached for reuse.
- `prompt_version` enables clean re-runs with improved prompts without ambiguity about which results are current.

## Image Pipeline

### Download

Beinecke serves Voynich folios via IIIF at predictable URLs. The batch script:
1. Reads a folio manifest (JSON list of herbal section folio IDs, ~130 entries).
2. Fetches each at 1024px width (sufficient for plant identification, not full 6000px archival scan).
3. Writes to UC Volume at `voynich/images/herbal/{folio_id}.jpg`.

### Vision Analysis Prompt

```
You are analyzing a folio from the Voynich Manuscript (MS 408), a 15th-century
document of unknown origin. This folio is from the herbal section.

Analyze this image and return a JSON object with exactly these fields:

1. "subject_candidates": Array of possible identifications for the plant depicted.
   For each: {"name": "common name", "latin": "Latin binomial if known",
   "confidence": 0.0-1.0, "reasoning": "brief visual evidence"}
   Include 1-3 candidates ranked by confidence.

2. "spatial_layout": {"text_regions": [...]} where each region has:
   {"position": "top-left|top-right|bottom|left|right|interlinear",
    "role": "label|description|recipe|marginalia|unknown",
    "estimated_lines": number}

3. "visual_description": A 2-3 sentence description of the plant's visual
   features (leaf shape, root structure, flowers, colors, scale).

4. "botanical_features": ["serrated leaves", "thick taproot", "red berries", ...]
   Observable morphological features useful for identification.

Return ONLY the JSON object, no markdown.
```

Cost: ~130 FMAPI calls, estimated under $5 total.

## Grounder Agent

### Tool: `score_image_grounding`

```ts
parameters: z.object({
  decoded_text: z.string(),        // the hypothesis's decoded text sample
  section: z.string(),             // "herbal"
  source_language: z.string(),     // "latin", "italian", etc.
  folio_id: z.string().optional()  // specific folio, or score against all herbal folios
})
```

### Scoring Logic (no vision call)

1. Query `folio_vision_analysis` for matching section (or specific folio).
2. Check if `expected_terms` is cached for this `source_language`. If not, make one LLM call: *"The Voynich folio depicts {subject_candidates}. What words would a medieval {source_language} herbalist use to describe or name this plant? Include common names, Latin names, related medical/culinary terms."* Cache the result back to the table.
3. Tokenize `decoded_text`, compute overlap against `expected_terms`:
   - Exact match: full credit
   - Substring/stem match: partial credit
   - Semantic field match (e.g., "root" near a plant with prominent roots): partial credit
4. Return composite `grounding` score in [0, 1].

### Scoring Across Folios

When no `folio_id` is given, the grounder scores decoded text against every herbal folio's expected terms and returns the best match. This matters because the hypothesis doesn't know which folio its decoded text came from — the grounder finds the strongest alignment.

### Escalation (optional)

If `grounding > 0.5`, the grounder fetches the actual folio image from the UC Volume and makes a live vision call: *"Here is a Voynich folio and a proposed decoding: '{decoded_text}'. Does this decoding plausibly describe what's depicted? Score 0-1."* This is expensive but rare (~1-2% of hypotheses).

### Agent Instructions

```
You are the Voynich Grounder, a visual grounding specialist. Use the
score_image_grounding tool to evaluate whether a decoded text passage
matches what is depicted in the manuscript's illustrations.

When you receive a hypothesis, call the tool and respond with ONLY a
JSON object: { "grounding": <0-1> }
```

## Integration with Evolutionary Loop

### Config Changes

**`voynich-config.ts` — updated fitness weights:**

```ts
export const VOYNICH_FITNESS_WEIGHTS = {
  semantic: 0.15,      // demoted (hard-capped at 0.70)
  perplexity: 0.10,    // demoted (LLM hallucinated)
  consistency: 0.05,   // demoted (LLM hallucinated)
  statistical: 0.10,   // demoted (LLM hallucinated)
  adversarial: 0.10,   // kept (real signal from critic)
  grounding: 0.50,     // NEW — primary signal
};
```

**Orchestrator `app.yaml` — add grounder to fitness agents:**

```yaml
- name: FITNESS_AGENT_URLS
  value: "https://voynich-historian-....databricksapps.com,https://voynich-critic-....databricksapps.com,https://voynich-grounder-....databricksapps.com"
```

### What Changes

- Add `section: "herbal"` to hypothesis metadata (trivial — Phase 1 is herbal-only).
- Orchestrator adds grounder URL to `FITNESS_AGENT_URLS`.
- Updated fitness weights in `voynich-config.ts`.

### What Does NOT Change

- `evolutionary.ts` — loop already iterates over `fitnessAgents` generically.
- `pareto.ts` — already handles arbitrary fitness dimensions.
- `population.ts` — stores whatever fitness keys agents return.
- `hypothesis.ts` — `compositeFitness` reads weights dynamically.

## Deliverables

1. **Folio manifest** — JSON list of herbal section folio IDs with IIIF URLs.
2. **Image download script** — Downloads herbal folios to UC Volume.
3. **Vision analysis script** — Runs FMAPI vision over each image, writes to `folio_vision_analysis`.
4. **`voynich-grounder` agent** — Express app with `score_image_grounding` tool.
5. **Updated `voynich-config.ts`** — New fitness weights including `grounding`.
6. **Updated orchestrator config** — Grounder added to `FITNESS_AGENT_URLS`.
7. **SP + permissions setup** — New service principal for grounder with UC catalog/schema grants.
