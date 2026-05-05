"""Supervisor agent — normalizes AFR records and searches for candidates.

Default path: Vector Search (Databricks VS index).
Fallback path: SQL ILIKE search for records with initials or acronyms
               (single-letter tokens, all-caps abbreviations) that perform
               poorly under cosine/dot-product distance.
"""

from __future__ import annotations

import os
import re
from typing import Any

from apx_agent import LlmAgent, Dependencies

Workspace = Dependencies.Workspace

_INITIAL_RE = re.compile(r"\b[A-Z]\.\s*")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,5}\b")


def _escape(s: str) -> str:
    """Escape single quotes for SQL string literals."""
    return s.replace("'", "''")


def _is_abnormal(name: str) -> bool:
    """True if name contains initials (J.) or short all-caps abbreviations (LLC, ABC)."""
    return bool(_INITIAL_RE.search(name) or _ACRONYM_RE.search(name))


def _title_strip(s: str) -> str:
    return s.strip().title()


def normalize_record(
    name: str,
    address: str = "",
    account_number: str = "",
    ws: Workspace = None,
) -> dict[str, Any]:
    """Normalize an AFR applicant record and decide the search strategy.

    Returns a dict with normalized fields and 'strategy': 'vector' | 'sql'.
    name: applicant full name (raw, may have extra spaces or punctuation)
    address: service address (optional)
    account_number: utility account number (optional)"""
    normalized_name = _title_strip(name)
    normalized_address = _title_strip(address)
    normalized_account = account_number.strip()
    strategy = "sql" if _is_abnormal(name) else "vector"
    return {
        "name": normalized_name,
        "address": normalized_address,
        "account_number": normalized_account,
        "strategy": strategy,
    }


def vector_search(
    query: str,
    k: int = 10,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Search the Vector Search index for candidate utility account matches.

    Returns up to k candidates with similarity scores.
    query: normalized search string (name + address concatenated)
    k: number of candidates to return (default 10)"""
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        from .demo_data import vector_search_demo
        candidates = vector_search_demo(query, k)
        return {"candidates": candidates, "count": len(candidates), "source": "demo"}

    index_name = os.environ.get("VECTOR_SEARCH_INDEX_NAME", "")
    if not index_name:
        return {"error": "VECTOR_SEARCH_INDEX_NAME not configured", "candidates": [], "count": 0}

    columns = ["account_id", "name", "address", "account_number", "score"]
    raw = ws.vector_search_indexes.query_index(
        index_name=index_name,
        columns=columns,
        query_text=query,
        num_results=k,
    )

    col_names = [c.name for c in (raw.manifest.schema.columns or [])]
    rows = raw.result.data_array or []
    candidates = []
    for row in rows:
        record = dict(zip(col_names, row))
        candidates.append({
            "account_id": record.get("account_id", ""),
            "name": record.get("name", ""),
            "address": record.get("address", ""),
            "account_number": record.get("account_number", ""),
            "score": float(record.get("score", 0.0)),
        })

    return {"candidates": candidates, "count": len(candidates)}


def sql_search(
    name: str,
    address: str = "",
    ws: Workspace = None,
) -> dict[str, Any]:
    """Fallback SQL search for records that perform poorly under vector distance.

    Use for names with initials (J. Smith), acronyms (ABC LLC), or very short names.
    name: applicant name (may include initials or abbreviations)
    address: optional service address for narrowing results"""
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        from .demo_data import sql_search_demo
        candidates = sql_search_demo(name, address)
        return {"candidates": candidates, "count": len(candidates), "source": "demo"}

    table = os.environ.get("UTILITY_ACCOUNT_TABLE", "")
    if not table:
        return {"error": "UTILITY_ACCOUNT_TABLE not configured", "candidates": [], "count": 0}

    tokens = [_escape(t.strip(".,")) for t in name.split() if len(t.strip(".,")) > 1]
    name_conditions = " AND ".join(f"name ILIKE '%{t}%'" for t in tokens)
    addr_token = _escape(address.split()[0]) if address else ""
    address_clause = f"AND address ILIKE '%{addr_token}%'" if addr_token else ""

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
2. If strategy is "vector": call vector_search with "{name} {address}" as the query.
   If strategy is "sql": call sql_search with the name and address instead.
3. Review the candidates returned. If the count is 0, try the other search tool before giving up.
4. When you have a shortlist of candidates (up to 10), hand off to the evaluator.
   Call transfer_to_evaluator with a context summary that includes:
   - The normalized applicant record (name, address, account_number)
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
