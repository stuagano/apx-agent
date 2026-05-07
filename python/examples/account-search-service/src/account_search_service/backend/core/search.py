"""Account search — normalize, then fan out across VS indexes or fall back to SQL.

This module is the core of account-search-service and can also be imported directly
by other services that want to embed the search logic without an HTTP hop.
"""

from __future__ import annotations

import os
import re
from typing import Any

_INITIAL_RE = re.compile(r"\b[A-Z]\.\s*")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,5}\b")


def _is_abnormal(name: str) -> bool:
    return bool(_INITIAL_RE.search(name) or _ACRONYM_RE.search(name))


def normalize(name: str, address: str = "", account_number: str = "") -> dict[str, str]:
    """Normalize applicant name and address; choose search strategy.

    Returns a dict with normalized fields and 'strategy': 'vector' | 'sql'.
    """
    return {
        "name": name.strip().title(),
        "address": address.strip().title(),
        "account_number": account_number.strip(),
        "strategy": "sql" if _is_abnormal(name) else "vector",
    }


def vector_search(
    applicant_name: str,
    address: str = "",
    email: str = "",
    k: int = 10,
    tenant_id: str = "",
    ws: Any = None,
) -> dict[str, Any]:
    """Fan out across all three VS indexes; deduplicate by account_id keeping highest score."""
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        from .demo_data import vector_search_demo
        candidates = vector_search_demo(f"{applicant_name} {address}".strip(), k)
        return {"candidates": candidates, "count": len(candidates), "source": "demo"}

    indexes = {
        "full":        os.environ.get("VS_INDEX_FULL", ""),
        "last_addr":   os.environ.get("VS_INDEX_LAST_ADDR", ""),
        "first_email": os.environ.get("VS_INDEX_FIRST_EMAIL", ""),
    }
    missing = [n for n, v in indexes.items() if not v]
    if missing:
        return {"error": f"Missing VS index env vars: {missing}", "candidates": [], "count": 0}

    parts = applicant_name.strip().split()
    last_name = parts[-1] if parts else applicant_name
    first_name = parts[0] if len(parts) > 1 else applicant_name

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
            candidate = {
                "account_id": acct_id,
                "name": f"{record.get('first_name', '')} {record.get('last_name', '')}".strip(),
                "address": record.get("service_address_line1", ""),
                "account_number": record.get("account_number", ""),
                "score": score,
            }
            if acct_id not in seen or score > seen[acct_id]["score"]:
                seen[acct_id] = candidate

    candidates = sorted(seen.values(), key=lambda c: c["score"], reverse=True)
    return {"candidates": candidates, "count": len(candidates), "source": "live"}


def sql_search(
    name: str,
    address: str = "",
    ws: Any = None,
) -> dict[str, Any]:
    """ILIKE fallback for names with initials or acronyms that embed poorly."""
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
    sql = f"SELECT account_id, name, address FROM {table} WHERE {name_conditions} {address_clause} LIMIT 20"

    def _warehouse_id(workspace: Any) -> str:
        for wh in workspace.warehouses.list():
            if wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower():
                return wh.id or ""
        for wh in workspace.warehouses.list():
            if wh.id:
                return wh.id
        raise RuntimeError("No SQL warehouse available")

    from databricks.sdk.service.sql import StatementState
    result = ws.statement_execution.execute_statement(
        warehouse_id=_warehouse_id(ws),
        statement=sql,
        wait_timeout="30s",
    )
    if result.status is None or result.status.state != StatementState.SUCCEEDED:
        error_msg = result.status.error if result.status else "unknown"
        return {"error": f"SQL failed: {error_msg}", "candidates": [], "count": 0}

    cols = [c.name for c in (result.manifest.schema.columns or [])]
    candidates = [dict(zip(cols, r)) for r in (result.result.data_array or [])]
    return {"candidates": candidates, "count": len(candidates), "source": "live"}


def search(
    name: str,
    address: str = "",
    email: str = "",
    tenant_id: str = "",
    k: int = 10,
    account_number: str = "",
    ws: Any = None,
) -> dict[str, Any]:
    """Top-level search: normalize, choose strategy, execute, return results."""
    norm = normalize(name, address, account_number)
    if norm["strategy"] == "vector":
        result = vector_search(norm["name"], norm["address"], email, k, tenant_id, ws)
    else:
        result = sql_search(norm["name"], norm["address"], ws)
    return {
        **result,
        "strategy": norm["strategy"],
        "normalized_name": norm["name"],
        "normalized_address": norm["address"],
    }
