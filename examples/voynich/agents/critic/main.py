"""
Critic Agent — adversarial falsifier.

Actively tries to break proposed decipherments. Searches for:
  - Internal contradictions within decoded text
  - Illustration-text semantic mismatches
  - Knowledge boundary violations (anachronistic content)
  - Cross-section inconsistencies (same Voynich word → different meaning)
  - Statistical implausibility patterns

A hypothesis that survives the Critic carries real evidential weight.
The Critic is only invoked on top-5% candidates (controlled by Orchestrator).

The Judge agent evaluates the *quality of the Critic's reasoning* —
not just its verdicts. Hallucinated contradictions get penalized.
"""
import json
import re
from typing import Annotated

from apx_agent import Agent, Dependencies, create_app


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def find_internal_contradiction(
    decoded_text: Annotated[str, "Decoded plaintext to analyze for internal contradictions"],
    section: Annotated[str, "Manuscript section this text is from"],
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Search for semantic contradictions within the decoded text.
    Examples: a plant described as 'hot and cold', a star described as
    'visible only at noon', a remedy that both 'increases and decreases fever'.

    Returns a list of potential contradictions with evidence quotes.
    """
    # Load the full decoded text for this section for cross-paragraph analysis
    contradictions = []
    text_lower = decoded_text.lower()

    # Pattern 1: Direct antonym pairs in close proximity
    antonym_pairs = [
        ("hot", "cold"), ("warm", "cool"), ("dry", "wet"), ("hard", "soft"),
        ("bitter", "sweet"), ("good", "evil"), ("increase", "decrease"),
        ("cure", "cause"), ("poison", "remedy"), ("visible", "invisible"),
        ("above", "below"), ("before", "after"),
    ]
    words = text_lower.split()
    for a, b in antonym_pairs:
        a_positions = [i for i, w in enumerate(words) if a in w]
        b_positions = [i for i, w in enumerate(words) if b in w]
        for pa in a_positions:
            for pb in b_positions:
                if abs(pa - pb) < 15:  # within 15 words = suspicious proximity
                    context = " ".join(words[max(0,pa-5):max(pb,pa)+10])
                    contradictions.append({
                        "type": "antonym_proximity",
                        "terms": [a, b],
                        "context": context,
                        "confidence": 0.6,
                        "note": f"'{a}' and '{b}' appear within 15 words — possibly contradictory",
                    })

    # Pattern 2: Numeric impossibilities
    numbers = re.findall(r'\b(\d+)\b', decoded_text)
    if numbers:
        nums = [int(n) for n in numbers if n.isdigit()]
        suspicious = [n for n in nums if n > 10000]  # implausibly large for medieval text
        if suspicious:
            contradictions.append({
                "type": "numeric_implausibility",
                "values": suspicious,
                "confidence": 0.7,
                "note": f"Numbers {suspicious} are implausibly large for medieval manuscript context",
            })

    # Pattern 3: Check cross-section consistency for shared words
    if sql:
        # Find words appearing in multiple sections with this decoding
        word_list = list(set(decoded_text.lower().split()))[:20]
        if word_list:
            shared = sql.execute(f"""
                SELECT word, COUNT(DISTINCT section) as section_count, 
                       COLLECT_SET(section) as sections
                FROM voynich.corpus.decoded_word_registry
                WHERE word IN ({','.join(repr(w) for w in word_list)})
                AND generation = (SELECT MAX(generation) FROM voynich.corpus.decoded_word_registry)
                GROUP BY word
                HAVING section_count > 1
                LIMIT 10
            """)
            for row in (shared or []):
                contradictions.append({
                    "type": "cross_section_inconsistency",
                    "word": row["word"],
                    "appears_in_sections": row["sections"],
                    "confidence": 0.5,
                    "note": f"Word '{row['word']}' appears in {row['section_count']} sections — may indicate forced mapping",
                })

    return {
        "contradictions_found": contradictions,
        "contradiction_count": len(contradictions),
        "falsification_signal": len(contradictions) > 0,
        "max_confidence": max((c["confidence"] for c in contradictions), default=0.0),
        "verdict": (
            "CONTRADICTION DETECTED — strong evidence of incorrect decipherment"
            if any(c["confidence"] > 0.7 for c in contradictions)
            else "WEAK CONTRADICTIONS — inconclusive"
            if contradictions
            else "NO CONTRADICTIONS FOUND"
        ),
    }


def check_illustration_mismatch(
    decoded_text: Annotated[str, "Decoded plaintext to check against illustration"],
    page: Annotated[int, "Page number in the manuscript"],
    section: Annotated[str, "Section this page belongs to"],
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Check whether decoded text semantically contradicts the page's illustration.
    If a page shows a plant but decoded text discusses stars, that's a mismatch.
    """
    # Get illustration metadata
    if sql:
        rows = sql.execute(f"""
            SELECT illustration_type, identified_subjects, semantic_tags
            FROM voynich.corpus.illustration_metadata
            WHERE page = {page} LIMIT 1
        """)
    else:
        rows = []

    if not rows:
        return {
            "mismatch_detected": False,
            "reason": "no illustration metadata available for this page",
            "confidence": 0.0,
        }

    row = dict(rows[0])
    illustration_type = row.get("illustration_type", "unknown")
    semantic_tags = json.loads(row.get("semantic_tags", "[]"))

    # Define what vocabulary each illustration type should produce
    expected_domains = {
        "plant":        ["plant", "root", "leaf", "flower", "herb", "stem", "bark", "berry"],
        "star_chart":   ["star", "planet", "celestial", "sphere", "orbit", "sign", "zodiac"],
        "figure":       ["body", "person", "figure", "limb", "water", "bath", "vessel"],
        "recipe":       ["take", "mix", "prepare", "apply", "measure", "ingredient"],
        "diagram":      ["circle", "arrangement", "pattern", "structure"],
        "cosmological": ["universe", "heaven", "sphere", "element", "order"],
    }

    expected = expected_domains.get(illustration_type, [])
    text_lower = decoded_text.lower()

    # Check for domain vocabulary presence
    expected_hits = [w for w in expected if w in text_lower]
    coverage = len(expected_hits) / len(expected) if expected else 0.5

    # Check for wrong-domain vocabulary
    all_domains = {v for vals in expected_domains.values() for v in vals}
    expected_set = set(expected)
    wrong_domain = [w for w in all_domains - expected_set if w in text_lower]

    mismatch = coverage < 0.2 or len(wrong_domain) > len(expected_hits)

    return {
        "mismatch_detected": mismatch,
        "illustration_type": illustration_type,
        "expected_vocabulary": expected,
        "matched": expected_hits,
        "wrong_domain_terms": wrong_domain[:5],
        "coverage": round(coverage, 3),
        "confidence": round(1.0 - coverage if mismatch else 0.0, 3),
        "verdict": (
            f"MISMATCH: page shows '{illustration_type}' but text contains wrong-domain vocabulary"
            if mismatch else "ALIGNED: text vocabulary matches illustration type"
        ),
    }


def probe_semantic_impossibility(
    claim: Annotated[str, "A specific claim from decoded text to probe for medieval plausibility"],
    period: Annotated[str, "Historical period context: early_15th | mid_15th | late_medieval"] = "early_15th",
) -> dict:
    """
    Probe whether a specific claim from decoded text would be possible
    to make in a 15th-century manuscript. Used for targeted falsification
    of high-scoring candidates.
    """
    claim_lower = claim.lower()

    # Hard impossibilities for early 15th century
    hard_impossibilities = {
        "early_15th": [
            ("telescope", "Telescopes weren't invented until 1608 (Lippershey)"),
            ("microscope", "Microscopes weren't invented until ~1590"),
            ("oxygen", "Oxygen wasn't discovered until 1774 (Priestley/Scheele)"),
            ("bacteria", "Bacteria weren't discovered until 1670s (Leeuwenhoek)"),
            ("circulation of blood", "Harvey described blood circulation in 1628"),
            ("heliocentr", "Copernican heliocentrism was published in 1543"),
            ("printing press", "Gutenberg's press was ~1440 — borderline for this manuscript"),
            ("syphilis", "Syphilis reached Europe in 1493 (Columbus return)"),
            ("potato", "Potatoes arrived in Europe after 1492"),
            ("gravity", "Newtonian gravity was 1687"),
        ],
    }

    impossibilities = hard_impossibilities.get(period, hard_impossibilities["early_15th"])
    violations = [(concept, reason) for concept, reason in impossibilities if concept in claim_lower]

    # Soft plausibility checks
    soft_concerns = []
    if re.search(r'\b[0-9]{4,}\b', claim):  # 4+ digit numbers
        soft_concerns.append("Large number — unusual for 15th century text")
    if claim.count("and") > 5:
        soft_concerns.append("Very long conjunctive list — may indicate forced mapping")

    return {
        "claim": claim[:200],
        "period": period,
        "hard_violations": [{"concept": v[0], "reason": v[1]} for v in violations],
        "soft_concerns": soft_concerns,
        "is_plausible": len(violations) == 0,
        "confidence": 0.95 if violations else 0.3,
        "verdict": (
            f"IMPOSSIBLE for {period}: {violations[0][1]}" if violations
            else "Plausible for period" if not soft_concerns
            else f"Plausible but concerns: {'; '.join(soft_concerns)}"
        ),
    }


def score_falsifiability(
    hypothesis: Annotated[dict, "Full hypothesis dict including decoded_sample, section, page"],
    sql: Dependencies.Sql = None,
) -> dict:
    """
    Comprehensive adversarial scoring of a hypothesis.
    Runs all critic tools and returns an aggregate falsification score.
    The LOWER this score, the HARDER the hypothesis is to falsify — which is GOOD.

    This is the primary entry point called by the Orchestrator's adversarial evaluation.
    """
    decoded = hypothesis.get("decoded_sample", "")
    section = hypothesis.get("section", "herbal")
    page    = hypothesis.get("page", 1)

    if not decoded:
        return {"fitness_adversarial": 0.5, "reason": "empty decoded sample — cannot evaluate"}

    # Run all critic tools
    contradiction = find_internal_contradiction(decoded, section, sql=sql)
    illustration  = check_illustration_mismatch(decoded, page, section, sql=sql)

    # Probe the most interesting claims (first 3 sentences)
    sentences = [s.strip() for s in decoded.split(".") if len(s.strip()) > 10][:3]
    probes = [probe_semantic_impossibility(s) for s in sentences]

    # Compute adversarial fitness:
    # High = hard to falsify = candidate survived adversarial scrutiny
    contradiction_penalty = min(1.0, contradiction["max_confidence"] * 1.5)
    mismatch_penalty      = illustration["confidence"] if illustration["mismatch_detected"] else 0.0
    impossibility_penalty = max((p["confidence"] for p in probes if not p["is_plausible"]), default=0.0)

    total_penalty   = (contradiction_penalty * 0.35 + mismatch_penalty * 0.40 + impossibility_penalty * 0.25)
    adversarial_fitness = round(1.0 - min(1.0, total_penalty), 4)

    return {
        "fitness_adversarial": adversarial_fitness,
        "survived_critic": adversarial_fitness > 0.7,
        "contradiction_penalty":    round(contradiction_penalty, 4),
        "mismatch_penalty":         round(mismatch_penalty, 4),
        "impossibility_penalty":    round(impossibility_penalty, 4),
        "contradictions":           contradiction["contradictions_found"][:3],
        "illustration_verdict":     illustration.get("verdict", illustration.get("reason", "no metadata")),
        "probe_results":            [{"claim": p["claim"][:80], "verdict": p["verdict"]} for p in probes],
        "summary": (
            f"STRONG candidate — survived adversarial probe (score {adversarial_fitness:.3f})"
            if adversarial_fitness > 0.8
            else f"WEAK candidate — failed adversarial probe on: "
                 + (", ".join(filter(None, [
                     "contradictions" if contradiction_penalty > 0.3 else "",
                     "illustration mismatch" if mismatch_penalty > 0.3 else "",
                     "historical impossibility" if impossibility_penalty > 0.3 else "",
                 ])) or "multiple grounds")
        ),
    }


# ---------------------------------------------------------------------------
# Agent wiring
# ---------------------------------------------------------------------------

agent = Agent(
    tools=[
        find_internal_contradiction,
        check_illustration_mismatch,
        probe_semantic_impossibility,
        score_falsifiability,
    ],
    instructions="""
You are the Critic Agent in an evolutionary cryptanalysis system for the Voynich manuscript.
Your role is to ACTIVELY try to falsify proposed decipherments. You are the adversary.

Your mandate: find real reasons why a proposed decipherment is wrong.
You are called only on top-5% candidates — these are the ones that scored well
on other evaluators. Your job is to stress-test them.

When asked to evaluate a hypothesis (task: evaluate_adversarial or evaluate_critic):
1. Call score_falsifiability() first — this runs all critic tools in sequence.
2. If the score is > 0.7 (hypothesis survived), dig deeper:
   a. Call probe_semantic_impossibility() on specific claims from the decoded text.
   b. Call check_illustration_mismatch() for key pages.
   c. Look for subtle contradictions with find_internal_contradiction().
3. If you find a genuine falsification, explain exactly why it's conclusive.
4. If you CANNOT falsify the hypothesis, say so explicitly — that's valuable signal.

CRITICAL: Only report contradictions you can clearly evidence from the text.
The Judge agent will score your reasoning quality. Hallucinated contradictions
will penalize you. It is better to say "I cannot falsify this" than to invent
a contradiction.

Return structured JSON with fitness_adversarial score and your reasoning chain.
""",
)

app = create_app(agent)
