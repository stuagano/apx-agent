"""Synthetic utility account data for demo/POC runs.

Used when DEMO_MODE=true so the agent can be demonstrated without a real
Vector Search index or SQL warehouse. Covers key entity resolution scenarios:
high-confidence match, familial match, nickname variants, initials/acronym
records (SQL path), and a low-confidence near-miss.
"""

from __future__ import annotations

ACCOUNTS: list[dict] = [
    # --- Standard residential accounts ---
    {"account_id": "DEN-001234", "name": "Jane Smith",       "address": "123 Maple Ave Denver CO",        "account_number": "DEN-001234"},
    {"account_id": "DEN-001235", "name": "John Smith",       "address": "123 Maple Ave Denver CO",        "account_number": "DEN-001235"},  # familial
    {"account_id": "DEN-002891", "name": "Elizabeth Rodriguez", "address": "456 Oak Street Aurora CO",    "account_number": "DEN-002891"},
    {"account_id": "DEN-002892", "name": "Liz Rodriguez",    "address": "456 Oak Street Apt 2 Aurora CO", "account_number": "DEN-002892"},  # nickname
    {"account_id": "DEN-003456", "name": "William Chen",     "address": "789 Pine Blvd Lakewood CO",      "account_number": "DEN-003456"},
    {"account_id": "DEN-003457", "name": "Bill Chen",        "address": "789 Pine Blvd Unit B Lakewood CO","account_number": "DEN-003457"},  # nickname
    {"account_id": "DEN-004123", "name": "Sarah Johnson",    "address": "234 Elm Drive Thornton CO",      "account_number": "DEN-004123"},
    {"account_id": "DEN-004124", "name": "Sara Johnson",     "address": "235 Elm Drive Thornton CO",      "account_number": "DEN-004124"},  # near-miss (diff house)
    {"account_id": "DEN-005678", "name": "Michael O'Brien",  "address": "567 Birch Lane Westminster CO",  "account_number": "DEN-005678"},
    {"account_id": "DEN-005679", "name": "M. O'Brien",       "address": "567 Birch Ln Westminster CO",    "account_number": "DEN-005679"},  # initials → SQL path
    {"account_id": "DEN-008901", "name": "Maria Garcia",     "address": "654 Willow Court Englewood CO",  "account_number": "DEN-008901"},
    {"account_id": "DEN-008902", "name": "Mary Garcia",      "address": "654 Willow Court Apt 3 Englewood CO", "account_number": "DEN-008902"},  # familial
    {"account_id": "DEN-009012", "name": "Robert Thompson",  "address": "987 Cedar Ave Arvada CO",        "account_number": "DEN-009012"},
    # --- Commercial / acronym accounts (SQL path) ---
    {"account_id": "DEN-006789", "name": "Southwest Energy Corp", "address": "890 Industrial Way Commerce City CO", "account_number": "DEN-006789"},
    {"account_id": "DEN-007890", "name": "Green Valley Power LLC", "address": "321 Valley Road Brighton CO",        "account_number": "DEN-007890"},
]


def _token_overlap_score(query: str, account: dict) -> float:
    """Simple scoring: fraction of query tokens found in account name+address."""
    q_tokens = set(query.lower().split())
    target = f"{account['name']} {account['address']}".lower()
    t_tokens = set(target.split())
    if not q_tokens:
        return 0.0
    overlap = q_tokens & t_tokens
    # Weight name matches higher than address matches
    name_tokens = set(account["name"].lower().split())
    name_hits = q_tokens & name_tokens
    score = (len(name_hits) * 2 + len(overlap - name_hits)) / (len(q_tokens) * 2)
    return min(round(score, 3), 1.0)


def vector_search_demo(query: str, k: int = 10) -> list[dict]:
    """Return top-k accounts scored by token overlap with the query."""
    scored = [
        {**acct, "score": _token_overlap_score(query, acct)}
        for acct in ACCOUNTS
    ]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return [s for s in scored[:k] if s["score"] > 0]


def sql_search_demo(name: str, address: str = "") -> list[dict]:
    """Return accounts whose name contains at least one non-trivial name token."""
    tokens = [t.strip(".,").lower() for t in name.split() if len(t.strip(".,")) > 1]
    results = []
    for acct in ACCOUNTS:
        acct_name = acct["name"].lower()
        if any(tok in acct_name for tok in tokens):
            results.append({**acct, "score": None})
    return results
