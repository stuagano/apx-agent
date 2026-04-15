"""
Historian Agent — medieval RAG and period-plausibility scoring.

Uses Databricks Vector Search to query medieval botanical, astronomical,
pharmaceutical, and alchemical corpora. Scores decoded text for:
  - Period-appropriate vocabulary (1400-1450 CE)
  - Knowledge boundary compliance (no post-Renaissance concepts)
  - Section-illustration semantic alignment

The Historian is the closest thing to a Rosetta Stone signal available.
If a decoded herbal section reads like Dioscorides, that's evidence.
"""
import json
import math
from typing import Annotated

from apx_agent import Agent, Dependencies, create_app


# ---------------------------------------------------------------------------
# Vector Search indexes (configured at deployment time)
# ---------------------------------------------------------------------------

VECTOR_INDEXES = {
    "botanical":       "voynich.medieval.botanical_index",      # Dioscorides, Hildegard, Apuleius
    "astronomical":    "voynich.medieval.astronomical_index",    # Ptolemy, Arabic star catalogs, Sacrobosco
    "pharmaceutical":  "voynich.medieval.pharmaceutical_index",  # Antidotarium Nicolai, Circa instans
    "alchemical":      "voynich.medieval.alchemical_index",      # Pseudo-Lull, Jabir corpus
    "general":         "voynich.medieval.general_index",         # Combined medieval Latin texts
}

SECTION_TO_INDEX = {
    "herbal":          "botanical",
    "astronomical":    "astronomical",
    "balneological":   "pharmaceutical",
    "pharmaceutical":  "pharmaceutical",
    "recipes":         "alchemical",
    "cosmological":    "astronomical",
}

# Known anachronisms: concepts that didn't exist pre-1440
POST_RENAISSANCE_CONCEPTS = [
    "telescope", "microscope", "oxygen", "carbon", "bacteria", "virus",
    "circulation", "gravity", "heliocentr", "copernican", "newtonian",
    "logarithm", "calculus", "perspective",  # perspective was late 15th
    "printing press", "movable type",        # Gutenberg c.1440
    "syphilis",                              # arrived in Europe 1493
    "potato", "tomato", "corn", "tobacco",  # new world plants
]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def search_medieval_corpus(
    query: Annotated[str, "Natural language query to search against medieval texts"],
    corpus: Annotated[str, "Which corpus to search: botanical | astronomical | pharmaceutical | alchemical | general"],
    top_k: Annotated[int, "Number of results to return"] = 10,
    ws: Dependencies.Workspace = None,
) -> dict:
    """
    Semantic search over indexed medieval texts using Databricks Vector Search.
    Returns ranked passages with source, date, and similarity score.
    """
    index_name = VECTOR_INDEXES.get(corpus, VECTOR_INDEXES["general"])
    try:
        results = ws.vector_search_indexes.query_index(
            index_name=index_name,
            columns=["text", "source", "author", "date_ce", "language", "section_type"],
            query_text=query,
            num_results=top_k,
        )
        return {
            "corpus": corpus,
            "query": query,
            "results": [
                {
                    "text": r.get("text", "")[:300],
                    "source": r.get("source", ""),
                    "author": r.get("author", ""),
                    "date_ce": r.get("date_ce", ""),
                    "score": r.get("score", 0.0),
                }
                for r in (results.result.data_array or [])
            ],
        }
    except Exception as e:
        return {"error": str(e), "corpus": corpus, "query": query}


def score_period_vocabulary(
    decoded_text: Annotated[str, "Decoded/candidate plaintext to evaluate"],
    section: Annotated[str, "Manuscript section: herbal | astronomical | balneological | pharmaceutical | recipes"],
    ws: Dependencies.Workspace = None,
) -> dict:
    """
    Score decoded text against period-appropriate vocabulary for the given section.
    Uses cosine similarity to the medieval corpus index for that section type.
    Returns a fitness score 0-1 and the top matching passages.
    """
    corpus_key = SECTION_TO_INDEX.get(section, "general")

    # Query the vector index with the decoded text as the query
    results = search_medieval_corpus(
        query=decoded_text[:500],
        corpus=corpus_key,
        top_k=5,
        ws=ws,
    )

    if "error" in results or not results.get("results"):
        return {"score": 0.0, "reason": "vector search failed or no results"}

    top_scores = [r["score"] for r in results["results"]]
    avg_score = sum(top_scores) / len(top_scores) if top_scores else 0.0
    best_score = max(top_scores) if top_scores else 0.0

    return {
        "score": round((avg_score * 0.4 + best_score * 0.6), 4),
        "section": section,
        "corpus_used": corpus_key,
        "top_match": results["results"][0] if results["results"] else None,
        "avg_similarity": round(avg_score, 4),
        "best_similarity": round(best_score, 4),
    }


def check_anachronism(
    decoded_text: Annotated[str, "Decoded text to check for post-medieval concepts"],
    strictness: Annotated[str, "How strict: strict (1400 CE) | moderate (1450 CE) | lenient (1500 CE)"] = "moderate",
) -> dict:
    """
    Flag any concepts in decoded text that post-date the manuscript's likely
    composition (early 15th century). Anachronisms are strong evidence of
    a bad decipherment.
    """
    text_lower = decoded_text.lower()
    found = []

    for concept in POST_RENAISSANCE_CONCEPTS:
        if concept in text_lower:
            found.append(concept)

    # Additional heuristic checks
    warnings = []
    if any(c.isdigit() for c in decoded_text):
        warnings.append("Contains Arabic numerals — suspicious for 15th century manuscript text")
    if "%" in decoded_text or "=" in decoded_text:
        warnings.append("Contains symbols anachronistic to 15th century text")

    # penalty grows with violations: 0 = no penalty, 1 = maximum
    violation_severity = min(1.0, len(found) * 0.50 + len(warnings) * 0.20)
    penalty = {
        "strict":   min(1.0, violation_severity * 1.50),
        "moderate": violation_severity,
        "lenient":  violation_severity * 0.40,
    }[strictness]

    return {
        "anachronisms_found": found,
        "warnings": warnings,
        "anachronism_free": len(found) == 0 and len(warnings) == 0,
        "fitness_penalty": round(penalty, 4) if found or warnings else 0.0,
        "recommendation": (
            "Strong evidence of bad decipherment — anachronistic content" if found
            else "No obvious anachronisms detected"
        ),
    }


def get_illustration_semantic_hints(
    page: Annotated[int, "Manuscript page number (1-240)"],
    section: Annotated[str, "Section: herbal | astronomical | balneological | pharmaceutical | recipes"],
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Return semantic hints from illustration metadata for a given page.
    These are the 'ground truth' signals: if a page shows a plant with
    red flowers, decoded text near that illustration should mention
    botanical/color/medicinal concepts.
    """
    rows = sql.execute(f"""
        SELECT
            page,
            section,
            illustration_type,
            identified_subjects,
            color_palette,
            semantic_tags,
            scholarly_interpretation
        FROM voynich.corpus.illustration_metadata
        WHERE page = {page}
        LIMIT 1
    """)
    if not rows:
        return {"page": page, "section": section, "error": "no illustration metadata for this page"}

    row = dict(rows[0])
    tags = json.loads(row.get("semantic_tags", "[]"))

    return {
        "page": page,
        "section": section,
        "illustration_type": row.get("illustration_type"),
        "identified_subjects": row.get("identified_subjects"),
        "semantic_tags": tags,
        "expected_vocabulary_domain": {
            "herbal":        ["plant", "root", "flower", "leaf", "remedy", "herb", "decoction"],
            "astronomical":  ["star", "planet", "sign", "zodiac", "constellation", "celestial"],
            "balneological": ["bath", "water", "vessel", "body", "nymph", "pool", "tub"],
            "pharmaceutical": ["preparation", "recipe", "ingredient", "measure", "compound"],
            "recipes":       ["take", "mix", "apply", "prepare", "dry", "grind"],
        }.get(section, []),
        "scholarly_interpretation": row.get("scholarly_interpretation"),
    }


def score_illustration_alignment(
    decoded_text: Annotated[str, "Decoded/candidate plaintext for this page"],
    page: Annotated[int, "Manuscript page number"],
    section: Annotated[str, "Manuscript section"],
    ws: Dependencies.Workspace = None,
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Score how well decoded text aligns with the page's illustration.
    This is the cross-modal fitness signal: illustrations constrain what
    the text should be about. High alignment = strong evidence of correct decipherment.
    """
    hints = get_illustration_semantic_hints(page, section, sql)
    if "error" in hints:
        return {"score": 0.5, "reason": "no illustration data for page"}

    expected_vocab = hints.get("expected_vocabulary_domain", [])
    if not expected_vocab:
        return {"score": 0.5, "reason": "no expected vocabulary for section"}

    text_lower = decoded_text.lower()
    matched = [w for w in expected_vocab if w in text_lower]
    coverage = len(matched) / len(expected_vocab)

    # Also do a vector similarity check
    semantic_score = score_period_vocabulary(decoded_text, section, ws=ws)
    combined = 0.5 * coverage + 0.5 * semantic_score.get("score", 0.0)

    return {
        "score": round(combined, 4),
        "page": page,
        "section": section,
        "expected_vocabulary": expected_vocab,
        "matched_terms": matched,
        "coverage_pct": round(coverage * 100, 1),
        "semantic_similarity": semantic_score.get("score", 0.0),
    }


def score_historian_fitness(
    hypothesis: Annotated[dict, "Full hypothesis dict including decoded_sample and section"],
    ws: Dependencies.Workspace = None,
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Composite historian fitness score for a hypothesis.
    Combines vocabulary score + anachronism penalty + illustration alignment.
    This is the primary entry point called by the Orchestrator's evaluation loop.
    """
    decoded = hypothesis.get("decoded_sample", "")
    section = hypothesis.get("section", "herbal")
    page = hypothesis.get("page", 1)

    if not decoded:
        return {"fitness_historian": 0.0, "reason": "empty decoded sample"}

    vocab_score   = score_period_vocabulary(decoded, section, ws=ws)
    anachronism   = check_anachronism(decoded)
    illustration  = score_illustration_alignment(decoded, page, section, ws=ws, sql=sql)

    # Composite: vocabulary * (1 - anachronism_penalty) * illustration_alignment
    penalty = anachronism.get("fitness_penalty", 0.0)
    composite = (
        vocab_score.get("score", 0.0) * 0.40
        + (1.0 - penalty) * 0.25
        + illustration.get("score", 0.0) * 0.35
    )

    return {
        "fitness_historian": round(composite, 4),
        "vocabulary_score":  vocab_score.get("score", 0.0),
        "anachronism_penalty": penalty,
        "illustration_alignment": illustration.get("score", 0.0),
        "anachronisms_found": anachronism.get("anachronisms_found", []),
        "top_medieval_match": vocab_score.get("top_match"),
    }


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

agent = Agent(
    tools=[
        search_medieval_corpus,
        score_period_vocabulary,
        check_anachronism,
        get_illustration_semantic_hints,
        score_illustration_alignment,
        score_historian_fitness,
    ],
    instructions="""
You are the Historian Agent in an evolutionary cryptanalysis system for the Voynich manuscript.
Your role is to score proposed decipherments for historical and semantic plausibility.

When asked to evaluate a hypothesis (task: evaluate_historian):
1. Call score_historian_fitness() with the hypothesis dict. This is the primary tool.
2. If the composite score is > 0.5, call search_medieval_corpus() to find the
   closest matching medieval text passages and include them in your response.
3. If anachronisms are found, explain specifically why they indicate a bad decipherment.
4. Return a JSON object with your fitness score and supporting evidence.

Key principles:
- The manuscript was likely composed 1404-1438 CE, probably in Italy.
- The Herbal section should decode to plant/remedy vocabulary (Dioscorides-style).
- The Astronomical section should decode to star/constellation/zodiac vocabulary.
- The Balneological section should decode to bathing/body/water vocabulary.
- NO post-1440 CE concepts should appear in valid decoded text.
- Illustration alignment is the strongest signal — use get_illustration_semantic_hints().

Always return structured JSON. Log your reasoning as MLflow span tags.
""",
)

app = create_app(agent)
