"""
Decipherer Agent — hypothesis generator and mutator.

Proposes new cipher hypotheses given parent hypotheses and their failure modes.
Every tool is a plain Python function; type hints become the schema;
Dependencies.* params are injected by FastAPI and excluded from the tool schema.

Deploy as a Databricks App:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
import json
import os
import re
import uuid
from collections import Counter
from typing import Annotated

from apx_agent import Agent, Dependencies, create_app
from pydantic import BaseModel, Field

_CATALOG = os.getenv("VOYNICH_CATALOG", "serverless_stable_s0v155_catalog")


# ---------------------------------------------------------------------------
# EVA alphabet — the standard Voynich transliteration scheme
# ---------------------------------------------------------------------------

EVA_CHARS = set("abcdefghijklmnopqrstuvwxyz")
EVA_COMMON = ["o", "a", "i", "n", "s", "e", "l", "r", "ch", "sh", "th", "q"]

CIPHER_TYPES = [
    "substitution",      # symbol → letter, 1:1
    "polyalphabetic",    # position-dependent substitution
    "null_bearing",      # some symbols are noise/nulls
    "transposition",     # symbols reordered within words
    "composite",         # combination of above
    "steganographic",    # meaning encoded in structure, not symbols
]

SOURCE_LANGUAGES = [
    "latin", "hebrew", "arabic", "italian",
    "occitan", "catalan", "greek", "czech",
]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def query_eva_corpus(
    section: Annotated[str, "Section name: herbal | astronomical | balneological | pharmaceutical | recipes | all"],
    sql: Dependencies.Sql,
) -> dict:
    """
    Query statistical properties of the EVA-transliterated corpus for a given section.
    Returns character frequencies, word frequencies, avg word length, token count.
    """
    section_filter = "" if section == "all" else f"WHERE section = '{section}f'"
    rows = sql.execute(ff""f"
        SELECT
            symbol,
            COUNT(*) as freq,
            section
        FROM {_CATALOG}.voynich_corpus.eva_chars
        {section_filter}
        GROUP BY symbol, section
        ORDER BY freq DESC
        LIMIT 50
    """)
    word_rows = sql.execute(ff""f"
        SELECT
            word,
            COUNT(*) as freq,
            AVG(LENGTH(word)) as avg_len
        FROM {_CATALOG}.voynich_corpus.eva_words
        {section_filter}
        GROUP BY word
        ORDER BY freq DESC
        LIMIT 100
    """)
    return {
        "section": section,
        "symbol_frequencies": [dict(r) for r in rows],
        "word_frequencies": [dict(r) for r in word_rows],
        "total_symbols": sum(r["freq"] for r in rows),
    }


def get_symbol_statistics(
    section: Annotated[str, "Section to analyze, or 'allf'"] = "all",
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Compute cryptographic statistics on EVA text: index of coincidence,
    bigram entropy, word length distribution. Used to assess cipher type.
    """
    rows = sql.execute(ff""f"
        SELECT symbol, COUNT(*) as freq
        FROM {_CATALOG}.voynich_corpus.eva_chars
        {'WHERE section = ' + repr(section) if section != 'all' else ''}
        GROUP BY symbol
    """)
    freqs = {r["symbol"]: r["freq"] for r in rows}
    total = sum(freqs.values())

    # Index of coincidence
    ic = sum(f * (f - 1) for f in freqs.values()) / (total * (total - 1)) if total > 1 else 0

    # Character entropy
    import math
    entropy = -sum((f/total) * math.log2(f/total) for f in freqs.values() if f > 0)

    return {
        "section": section,
        "unique_symbols": len(freqs),
        "total_symbols": total,
        "index_of_coincidence": round(ic, 4),
        "character_entropy_bits": round(entropy, 3),
        "top_symbols": sorted(freqs.items(), key=lambda x: -x[1])[:20],
        "ic_interpretation": (
            "monoalphabetic substitution likely" if ic > 0.065
            else "polyalphabetic or transposition likely" if ic > 0.04
            else "hoax or natural language likely"
        ),
    }


def apply_cipher(
    symbol_map: Annotated[dict, "Dict mapping EVA chars/strings to target alphabet chars"],
    null_chars: Annotated[list[str], "EVA chars to treat as nulls (strip before decode)"],
    text: Annotated[str, "Raw EVA text to decode"],
    reverse_word_order: Annotated[bool, "Apply within-word letter reversal"] = False,
) -> dict:
    """
    Apply a cipher hypothesis to a sample of EVA text.
    Returns decoded text and coverage metrics (% of symbols mapped).
    """
    # Strip nulls
    for null in null_chars:
        text = text.replace(null, "")

    # Sort map keys longest-first to handle multi-char tokens
    decoded = text
    for src, tgt in sorted(symbol_map.items(), key=lambda x: -len(x[0])):
        decoded = decoded.replace(src, tgt)

    # Reverse words if requested (right-to-left source language)
    if reverse_word_order:
        words = decoded.split()
        decoded = " ".join(w[::-1] for w in words)

    # Compute coverage
    original_chars = set(text.replace(" ", ""))
    mapped_chars = set(symbol_map.keys())
    coverage = len(original_chars & mapped_chars) / len(original_chars) if original_chars else 0

    return {
        "decoded_text": decoded[:1000],  # first 1000 chars
        "coverage_pct": round(coverage * 100, 1),
        "unmapped_chars": list(original_chars - mapped_chars),
        "null_chars_stripped": null_chars,
    }


def propose_initial_population(
    n: Annotated[int, "Number of seed hypotheses to generate"] = 50,
) -> list[dict]:
    """
    Generate N diverse seed hypotheses for generation 0.
    Covers all cipher types and source languages combinatorially.
    Returns a list of Hypothesis dicts ready for Delta insertion.
    """
    seeds = []
    import random
    for i in range(n):
        cipher_type = CIPHER_TYPES[i % len(CIPHER_TYPES)]
        language = SOURCE_LANGUAGES[i % len(SOURCE_LANGUAGES)]

        # Random symbol map (placeholder — LLM loop will refine these)
        alphabet = "abcdefghijklmnopqrstuvwxyz"
        shuffled = list(alphabet)
        random.shuffle(shuffled)
        symbol_map = {eva: shuffled[j % 26] for j, eva in enumerate(EVA_COMMON)}

        seeds.append({
            "id": str(uuid.uuid4())[:8],
            "generation": 0,
            "parent_id": None,
            "cipher_type": cipher_type,
            "source_language": language,
            "symbol_map": symbol_map,
            "null_chars": random.sample(EVA_COMMON, k=min(3, len(EVA_COMMON))),
            "transformation_rules": [],
        })

    return seeds


def validate_hypothesis(
    hypothesis: Annotated[dict, "Hypothesis dict to validate against schema"],
) -> dict:
    """
    Validate a proposed cipher hypothesis against the schema.
    Returns {'valid': bool, 'errors': list[str]}.
    """
    errors = []
    required = ["cipher_type", "source_language", "symbol_map"]
    for field in required:
        if field not in hypothesis:
            errors.append(f"missing required field: {field}")

    if "cipher_type" in hypothesis and hypothesis["cipher_type"] not in CIPHER_TYPES:
        errors.append(f"invalid cipher_type: {hypothesis['cipher_type']}. Must be one of {CIPHER_TYPES}")

    if "source_language" in hypothesis and hypothesis["source_language"] not in SOURCE_LANGUAGES:
        errors.append(f"invalid source_language: {hypothesis['source_language']}")

    if "symbol_map" in hypothesis:
        sm = hypothesis["symbol_map"]
        if not isinstance(sm, dict):
            errors.append("symbol_map must be a dict")
        elif len(sm) < 3:
            errors.append("symbol_map has fewer than 3 mappings — too sparse")
        elif len(set(sm.values())) < len(sm) * 0.5:
            errors.append("symbol_map maps too many symbols to the same target — check for homophones")

    return {"valid": len(errors) == 0, "errors": errors}


def get_failure_mode_analysis(
    fitness_vector: Annotated[dict, "Dict of fitness scores: {statistical, perplexity, semantic, consistency}"],
) -> dict:
    """
    Analyze a hypothesis's fitness vector to identify the primary failure mode.
    Returns a structured explanation to guide the mutation LLM.
    """
    analyses = {
        "statistical": {
            "score": fitness_vector.get("statistical", 0),
            "failure": "character frequency distribution doesn't match target language",
            "mutation_hint": "try a different source language or add/remove null characters",
        },
        "perplexity": {
            "score": fitness_vector.get("perplexity", 0),
            "failure": "decoded text has high perplexity — not plausible in any target language",
            "mutation_hint": "revisit source language assignment or try polyalphabetic approach",
        },
        "semantic": {
            "score": fitness_vector.get("semantic", 0),
            "failure": "decoded text doesn't semantically align with section illustrations",
            "mutation_hint": "focus mapping on section-specific vocabulary first (e.g. plant names for herbal section)",
        },
        "consistency": {
            "score": fitness_vector.get("consistency", 0),
            "failure": "same Voynich word decodes inconsistently across the manuscript",
            "mutation_hint": "check for accidental homophone mappings or unstable symbol boundaries",
        },
    }
    worst = min(analyses.items(), key=lambda x: x[1]["score"])
    return {
        "primary_failure_mode": worst[0],
        "detail": worst[1],
        "all_scores": {k: v["score"] for k, v in analyses.items()},
        "recommended_mutation_strategy": worst[1]["mutation_hint"],
    }


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

agent = Agent(
    tools=[
        query_eva_corpus,
        get_symbol_statistics,
        apply_cipher,
        propose_initial_population,
        validate_hypothesis,
        get_failure_mode_analysis,
    ],
    instructions="""
You are the Decipherer Agent in an evolutionary cryptanalysis system targeting
the Voynich manuscript. Your job is to generate and mutate cipher hypotheses.

When asked to generate mutations:
1. First call get_failure_mode_analysis() on each parent's fitness vector.
2. Generate N variants that specifically address the identified failure mode.
3. For each variant, call validate_hypothesis() before returning it.
4. Aim for diversity: spread mutations across cipher_type and source_language.
5. Return a JSON array of validated hypothesis dicts.

When generating the initial population:
- Use propose_initial_population() as a baseline.
- Enrich with EVA statistics from query_eva_corpus().
- Ensure coverage of all cipher types and multiple source languages.

Always return structured JSON. Never return prose explanations as the response body.
""",
)

app = create_app(agent)
