"""Supervisor agent — normalizes AFR records and searches for candidates.

Default path: Vector Search across three indexes (full name+address, last name+address,
              first name+email) to cover standard, familial, and maiden-name match cases.
Fallback path: SQL ILIKE search for records with initials or acronyms (single-letter tokens,
               all-caps abbreviations) that embed poorly under cosine/dot-product distance.
"""

from __future__ import annotations

import os
import re
from typing import Any

from apx_agent import LlmAgent, Dependencies

Workspace = Dependencies.Workspace

_INITIAL_RE = re.compile(r"\b[A-Z]\.\s*")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,5}\b")


def _is_abnormal(name: str) -> bool:
    """True if name contains initials (J.) or short all-caps abbreviations (LLC, ABC)."""
    return bool(_INITIAL_RE.search(name) or _ACRONYM_RE.search(name))


def normalize_record(
    name: str,
    address: str = "",
    account_number: str = "",
    ws: Workspace = None,
) -> dict[str, Any]:
    """Normalize an AFR applicant record and decide the search strategy.

    Returns normalized fields plus 'strategy': 'vector' | 'sql'.
    name: applicant full name (raw, may have extra spaces or punctuation)
    address: service address (optional)
    account_number: utility account number (optional)"""
    normalized_name = name.strip().title()
    normalized_address = address.strip().title()
    normalized_account = account_number.strip()
    strategy = "sql" if _is_abnormal(name) else "vector"
    return {
        "name": normalized_name,
        "address": normalized_address,
        "account_number": normalized_account,
        "strategy": strategy,
    }


def vector_search(
    applicant_name: str,
    address: str = "",
    email: str = "",
    k: int = 10,
    tenant_id: str = "",
    ws: Workspace = None,
) -> dict[str, Any]:
    """Search all three VS indexes for candidate utility account matches.

    Fans out across embed_full (full name+address), embed_last_addr (familial),
    and embed_first_email (maiden name) indexes, then deduplicates by account_id
    keeping the highest score per candidate.

    applicant_name: normalized full name (e.g. "Jane Smith")
    address: normalized service address (used in full and last-addr queries)
    email: optional email address (used in first-email query)
    k: candidates to retrieve per index (total before dedup may be up to 3k)
    tenant_id: optional hybrid filter — restricts to a single utility tenant"""
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        from .demo_data import vector_search_demo
        query = f"{applicant_name} {address}".strip()
        candidates = vector_search_demo(query, k)
        return {"candidates": candidates, "count": len(candidates), "source": "demo"}

    indexes = {
        "full":        os.environ.get("VS_INDEX_FULL", ""),
        "last_addr":   os.environ.get("VS_INDEX_LAST_ADDR", ""),
        "first_email": os.environ.get("VS_INDEX_FIRST_EMAIL", ""),
    }
    missing = [name for name, val in indexes.items() if not val]
    if missing:
        return {"error": f"Missing VS index env vars: {missing}", "candidates": [], "count": 0}

    name_parts = applicant_name.strip().split()
    last_name = name_parts[-1] if name_parts else applicant_name
    first_name = name_parts[0] if len(name_parts) > 1 else applicant_name

    queries = {
        "full":        f"{applicant_name} {address}".strip(),
        "last_addr":   f"{last_name} {address}".strip(),
        "first_email": f"{first_name} {email}".strip(),
    }

    filters = {"tenant_id": tenant_id} if tenant_id else {}
    columns = ["account_id", "first_name", "last_name", "service_address_line1", "account_number", "score"]

    seen: dict[str, dict] = {}
    for perm, index_name in indexes.items():
        raw = ws.vector_search_indexes.query_index(
            index_name=index_name,
            columns=columns,
            query_text=queries[perm],
            num_results=k,
            filters_json=str(filters) if filters else None,
        )
        col_names = [c.name for c in (raw.manifest.schema.columns or [])]
        for row in (raw.result.data_array or []):
            record = dict(zip(col_names, row))
            acct_id = record.get("account_id", "")
            score = float(record.get("score", 0.0))
            name = f"{record.get('first_name', '')} {record.get('last_name', '')}".strip()
            candidate = {
                "account_id": acct_id,
                "name": name,
                "address": record.get("service_address_line1", ""),
                "account_number": record.get("account_number", ""),
                "score": score,
            }
            if acct_id not in seen or score > seen[acct_id]["score"]:
                seen[acct_id] = candidate

    candidates = sorted(seen.values(), key=lambda c: c["score"], reverse=True)
    return {"candidates": candidates, "count": len(candidates)}


def sql_search(
    name: str,
    address: str = "",
    ws: Workspace = None,
) -> dict[str, Any]:
    """Fallback SQL search for records that embed poorly under vector distance.

    Use for names with initials (J. Smith), acronyms (ABC LLC), or very short tokens.
    name: applicant name (may include initials or abbreviations)
    address: optional service address for narrowing results"""
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        from .demo_data import sql_search_demo
        candidates = sql_search_demo(name, address)
        return {"candidates": candidates, "count": len(candidates), "source": "demo"}

    table = os.environ.get("UTILITY_ACCOUNT_TABLE", "")
    if not table:
        return {"error": "UTILITY_ACCOUNT_TABLE not configured", "candidates": [], "count": 0}

    tokens = [t.strip(".,") for t in name.split() if len(t.strip(".,")) > 1]
    name_conditions = " AND ".join(f"name ILIKE '%{t}%'" for t in tokens)
    address_clause = f"AND address ILIKE '%{address.split()[0]}%'" if address else ""

    sql = f"""
        SELECT account_id, name, address
        FROM {table}
        WHERE {name_conditions} {address_clause}
        LIMIT 20
    """

    def _get_warehouse_id(workspace: Any) -> str:
        for wh in workspace.warehouses.list():
            if wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower():
                return wh.id or ""
        for wh in workspace.warehouses.list():
            if wh.id:
                return wh.id
        raise RuntimeError("No SQL warehouse available")

    from databricks.sdk.service.sql import StatementState
    result = ws.statement_execution.execute_statement(
        warehouse_id=_get_warehouse_id(ws),
        statement=sql,
        wait_timeout="30s",
    )
    if result.status is None or result.status.state != StatementState.SUCCEEDED:
        error_msg = result.status.error if result.status else "unknown"
        return {"error": f"SQL failed: {error_msg}", "candidates": [], "count": 0}

    cols = [c.name for c in (result.manifest.schema.columns or [])]
    rows = result.result.data_array or []
    candidates = [dict(zip(cols, r)) for r in rows]
    return {"candidates": candidates, "count": len(candidates)}


SUPERVISOR_INSTRUCTIONS = """
You are the Supervisor in an entity resolution system for utility company AFR (Affordable Rate) applications.

Your job:
1. Call normalize_record on the applicant's name, address, and account number.
2. If strategy is "vector": call vector_search with applicant_name, address, email (if known),
   and tenant_id (from the application). This fans out across all three indexes (full name+address,
   last name+address, first name+email) and returns deduplicated candidates.
   If strategy is "sql": call sql_search with the name and address instead.
3. Review the candidates returned. If the count is 0, try the other search tool before giving up.
4. When you have a shortlist of candidates (up to 10), hand off to the evaluator.
   Call transfer_to_evaluator with a context summary that includes:
   - The normalized applicant record (name, address, account_number, email, tenant_id)
   - All candidates with their scores
   - Which search strategy was used and why

Do NOT attempt to make the enrollment decision yourself — that is the evaluator's role.
If the evaluator sends you a retry request with search hints, apply the hints and search again.
""".strip()

supervisor = LlmAgent(
    tools=[normalize_record, vector_search, sql_search],
    instructions=SUPERVISOR_INSTRUCTIONS,
    max_iterations=6,
)
