/**
 * Shared configuration for the Voynich decipherment evolutionary framework.
 *
 * Import these constants in any Voynich agent — mutation, fitness, judge, or orchestrator —
 * to ensure consistent fitness weights, objective names, and schema references.
 */

// ---------------------------------------------------------------------------
// Fitness weights
// ---------------------------------------------------------------------------

/**
 * Composite fitness weights for Voynich decipherment hypotheses.
 * Must sum to 1.0. Used by EvolutionaryAgent and PopulationStore.
 */
export const VOYNICH_FITNESS_WEIGHTS: Record<string, number> = {
  statistical: 0.25,
  perplexity: 0.25,
  semantic: 0.30,
  consistency: 0.15,
  adversarial: 0.05,
};

// ---------------------------------------------------------------------------
// Pareto objectives
// ---------------------------------------------------------------------------

/**
 * Objectives used for multi-objective Pareto selection.
 * A subset of VOYNICH_FITNESS_WEIGHTS keys — excludes adversarial
 * since it is a penalty rather than a primary objective.
 */
export const VOYNICH_PARETO_OBJECTIVES: string[] = [
  'statistical',
  'perplexity',
  'semantic',
  'consistency',
];

// ---------------------------------------------------------------------------
// Cipher and language enumerations
// ---------------------------------------------------------------------------

/**
 * Known cipher type hypotheses for Voynichese.
 */
export const VOYNICH_CIPHER_TYPES = [
  'substitution',
  'polyalphabetic',
  'null_bearing',
  'transposition',
  'composite',
  'steganographic',
] as const;

export type VoynichCipherType = (typeof VOYNICH_CIPHER_TYPES)[number];

/**
 * Source language candidates proposed in the scholarly literature.
 */
export const VOYNICH_SOURCE_LANGUAGES = [
  'latin',
  'hebrew',
  'arabic',
  'italian',
  'occitan',
  'catalan',
  'greek',
  'czech',
] as const;

export type VoynichSourceLanguage = (typeof VOYNICH_SOURCE_LANGUAGES)[number];

// ---------------------------------------------------------------------------
// EVA transliteration
// ---------------------------------------------------------------------------

/**
 * High-frequency characters in the EVA (European Voynich Alphabet) transliteration.
 * Used by statistical fitness agents to normalise character-frequency distributions.
 */
export const EVA_COMMON_CHARS: string[] = [
  'o', 'a', 'i', 'n', 's', 'e', 'l', 'r',
  'ch', 'sh', 'th', 'q',
];

// ---------------------------------------------------------------------------
// Anachronism detection
// ---------------------------------------------------------------------------

/**
 * Post-Renaissance concepts whose presence in a decoded text strongly indicates
 * a modern forgery or decipherment error. Adversarial fitness agents penalise
 * hypotheses that produce plaintext containing these terms.
 */
export const POST_RENAISSANCE_CONCEPTS: string[] = [
  'telescope',
  'microscope',
  'electricity',
  'oxygen',
  'nitrogen',
  'photosynthesis',
  'evolution',
  'chromosome',
  'antibiotic',
  'calculus',
  'bacteria',
  'virus',
  'vaccine',
  'radiation',
  'quantum',
  'relativity',
  'neuron',
  'dna',
  'rna',
  'protein',
];

// ---------------------------------------------------------------------------
// Databricks schema references
// ---------------------------------------------------------------------------

/**
 * Default Unity Catalog table used by PopulationStore to persist
 * decipherment hypotheses across generations.
 */
export const DEFAULT_POPULATION_TABLE = 'voynich.decipherment.population';

/**
 * Vector Search index names keyed by corpus name.
 * Corpus names correspond to the reference linguistic collections used
 * by fitness agents for semantic similarity scoring.
 */
export const VECTOR_INDEXES: Record<string, string> = {
  medieval_latin: 'voynich.decipherment.medieval_latin_vs_index',
  herbalism: 'voynich.decipherment.herbalism_vs_index',
  astrology: 'voynich.decipherment.astrology_vs_index',
  pharmacy: 'voynich.decipherment.pharmacy_vs_index',
  cosmology: 'voynich.decipherment.cosmology_vs_index',
};

/**
 * Maps Voynich manuscript sections to the most relevant reference corpus.
 * Used by semantic fitness agents to select the appropriate vector index.
 */
export const SECTION_TO_INDEX: Record<string, string> = {
  herbal: 'herbalism',
  astronomical: 'astrology',
  biological: 'medieval_latin',
  cosmological: 'cosmology',
  pharmaceutical: 'pharmacy',
  recipes: 'medieval_latin',
  stars: 'astrology',
};
