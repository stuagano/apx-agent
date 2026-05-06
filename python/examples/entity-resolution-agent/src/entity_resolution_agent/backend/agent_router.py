"""Entity resolution agent — single LlmAgent with full normalize→search→evaluate pipeline."""

from apx_agent import LlmAgent

from .core.supervisor import normalize_record, vector_search, sql_search
from .core.evaluator import evaluate_candidates, log_decision

_INSTRUCTIONS = """
You are an entity resolution agent. Your job is to match an incoming application to the
correct customer account in the database.

## Step 1 — Normalize
Call normalize_record on the applicant's name, address, and account number.
Returns: normalized_name, normalized_address, account_number, strategy ("vector" or "sql").

## Step 2 — Search
- strategy "vector": call vector_search with "{normalized_name} {normalized_address}" as the query.
- strategy "sql":    call sql_search with the name parts and address.
- If the first search returns 0 candidates, try the other tool before giving up.

## Step 3 — Evaluate
Call evaluate_candidates with:
- applicant: { name, address, account_number }
- candidates: the list from the search result

Returns a decision with:
- category: EXACT / HIGH_CONFIDENCE / LOW_CONFIDENCE / NO_MATCH
- matched_account_id: winning account ID (or null)
- confidence: 0.0–1.0
- rationale: plain-language explanation

## Step 4 — Log
Call log_decision with the full decision, then report clearly:
- Match category and confidence
- Matched account (name, address, account ID) or NO_MATCH
- Any edge cases (familial match, nickname, initials fallback)
- Flag LOW_CONFIDENCE results explicitly

Confidence thresholds: EXACT ≥ 0.90 | HIGH_CONFIDENCE ≥ 0.75 | LOW_CONFIDENCE ≥ 0.70 | NO_MATCH < 0.70
""".strip()

agent = LlmAgent(
    tools=[normalize_record, vector_search, sql_search, evaluate_candidates, log_decision],
    instructions=_INSTRUCTIONS,
    max_iterations=10,
)
