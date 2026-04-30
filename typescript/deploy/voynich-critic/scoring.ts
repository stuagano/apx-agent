/**
 * Pure scoring functions for the Voynich critic — extracted so calibration
 * scripts and tests can import them without booting the express server.
 *
 * Imported by app.ts (the running critic) and calibrate.ts (the sanity check).
 */

// ---------------------------------------------------------------------------
// Reference distributions
// ---------------------------------------------------------------------------

/** Top character bigrams in medieval Latin botanical text, by relative frequency. */
export const LATIN_BIGRAM_FREQ: Record<string, number> = {
  'um': 0.032, 'us': 0.029, 'is': 0.027, 'er': 0.026, 'in': 0.025,
  'it': 0.024, 'es': 0.023, 'am': 0.022, 'em': 0.021, 'at': 0.020,
  'en': 0.019, 'an': 0.019, 're': 0.018, 'nt': 0.018, 'ti': 0.017,
  'ra': 0.016, 'ur': 0.016, 'tu': 0.015, 'ta': 0.015, 'ae': 0.014,
  'et': 0.014, 'ar': 0.013, 'al': 0.013, 'de': 0.013, 'te': 0.012,
  'or': 0.012, 'ri': 0.012, 'li': 0.011, 'ro': 0.011, 'ni': 0.011,
  'co': 0.010, 'on': 0.010, 'ab': 0.010, 'la': 0.010, 'di': 0.010,
};

/** Common Latin word endings (suffixes) used for the morphological check. */
export const LATIN_ENDINGS = [
  'us', 'um', 'is', 'am', 'em', 'as', 'es', 'os', 'ae', 'arum',
  'orum', 'ibus', 'orum', 'ium', 'ens', 'ans', 'unt', 'unt',
  'tur', 'atur', 'ere', 'ire', 'are', 'alis', 'ilis', 'inus',
  'atus', 'itus', 'osis', 'onis', 'tio', 'tas', 'men', 'ment',
];

// ---------------------------------------------------------------------------
// Composite likelihood scorer
// ---------------------------------------------------------------------------

export interface LikelihoodBreakdown {
  likelihood: number;
  bigram_similarity: number;
  morphological_score: number;
  word_length_score: number;
  avg_word_length: number;
  words_with_latin_endings: number;
  total_words: number;
}

/**
 * Composite Latin-likelihood score for `text`. Combines:
 *   - 40% character-bigram cosine similarity vs LATIN_BIGRAM_FREQ
 *   - 35% fraction of words ending with a known Latin suffix
 *   - 25% word-length-distribution score (Latin averages 5-7 chars)
 *
 * Returns 0 with empty/short input; never throws.
 */
export function compositeLikelihood(text: string): LikelihoodBreakdown {
  const lowered = text.toLowerCase().replace(/[^a-z\s]/g, '');
  const chars = lowered.replace(/\s/g, '');

  const empty: LikelihoodBreakdown = {
    likelihood: 0,
    bigram_similarity: 0,
    morphological_score: 0,
    word_length_score: 0,
    avg_word_length: 0,
    words_with_latin_endings: 0,
    total_words: 0,
  };
  if (chars.length < 10) return empty;

  // Bigram cosine similarity vs reference
  const textBigrams: Record<string, number> = {};
  let totalBigrams = 0;
  for (let i = 0; i < chars.length - 1; i++) {
    const bg = chars.slice(i, i + 2);
    textBigrams[bg] = (textBigrams[bg] ?? 0) + 1;
    totalBigrams++;
  }
  let dotProduct = 0, normText = 0, normRef = 0;
  const allBigrams = new Set([...Object.keys(textBigrams), ...Object.keys(LATIN_BIGRAM_FREQ)]);
  for (const bg of allBigrams) {
    const tf = (textBigrams[bg] ?? 0) / totalBigrams;
    const rf = LATIN_BIGRAM_FREQ[bg] ?? 0;
    dotProduct += tf * rf;
    normText += tf * tf;
    normRef += rf * rf;
  }
  const bigramSimilarity = normText > 0 && normRef > 0
    ? dotProduct / (Math.sqrt(normText) * Math.sqrt(normRef))
    : 0;

  // Morphological ending check
  const words = lowered.split(/\s+/).filter((w) => w.length >= 3);
  const wordsWithLatinEnding = words.filter((w) =>
    LATIN_ENDINGS.some((ending) => w.endsWith(ending))
  );
  const morphScore = words.length > 0 ? wordsWithLatinEnding.length / words.length : 0;

  // Word-length distribution
  const avgWordLen = words.length > 0
    ? words.reduce((s, w) => s + w.length, 0) / words.length
    : 0;
  const wordLenScore = avgWordLen >= 4 && avgWordLen <= 8 ? 1.0 : avgWordLen >= 3 ? 0.5 : 0.2;

  return {
    likelihood: Math.round((bigramSimilarity * 0.4 + morphScore * 0.35 + wordLenScore * 0.25) * 1000) / 1000,
    bigram_similarity: Math.round(bigramSimilarity * 1000) / 1000,
    morphological_score: Math.round(morphScore * 1000) / 1000,
    word_length_score: Math.round(wordLenScore * 1000) / 1000,
    avg_word_length: Math.round(avgWordLen * 10) / 10,
    words_with_latin_endings: wordsWithLatinEnding.length,
    total_words: words.length,
  };
}
