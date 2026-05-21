"""Search client — calls account-search-service via HTTP when available, else searches locally.

Set SEARCH_SERVICE_URL to the deployed account-search-service URL for the HTTP path.
Leave it unset (or empty) to run Vector Search + SQL locally instead.
"""

from __future__ import annotations

import os
import re
from typing import Any

_INITIAL_RE = re.compile(r"\b[A-Z]\.\s*")
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,5}\b")


def _is_abnormal(name: str) -> bool:
    return bool(_INITIAL_RE.search(name) or _ACRONYM_RE.search(name))


def search_accounts(
    name: str,
    address: str = "",
    email: str = "",
    tenant_id: str = "",
    account_number: str = "",
    k: int = 10,
    ws: Any = None,
) -> dict[str, Any]:
    """Search utility accounts.

    If SEARCH_SERVICE_URL is configured, calls the account-search-service API (recommended
    for production — search scales independently). Otherwise runs VS/SQL locally.
    """
    service_url = os.environ.get("SEARCH_SERVICE_URL", "").rstrip("/")
    if service_url:
        return _call_search_service(service_url, name, address, email, tenant_id, k, ws)
    return _search_local(name, address, email, tenant_id, account_number, k, ws)


def _call_search_service(
    url: str,
    name: str,
    address: str,
    email: str,
    tenant_id: str,
    k: int,
    ws: Any,
) -> dict[str, Any]:
    import httpx
    headers: dict[str, str] = {}
    if ws:
        try:
            token = ws.config.token
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass

    resp = httpx.post(
        f"{url}/api/search",
        headers=headers,
        json={
            "applicant_name": name,
            "address": address,
            "email": email,
            "tenant_id": tenant_id,
            "k": k,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def _search_local(
    name: str,
    address: str,
    email: str,
    tenant_id: str,
    account_number: str,
    k: int,
    ws: Any,
) -> dict[str, Any]:
    """Local fallback — same logic as account-search-service, embedded here for single-app deploys."""
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        from demo_data import vector_search_demo, sql_search_demo
        if _is_abnormal(name):
            candidates = sql_search_demo(name, address)
            return {"candidates": candidates, "count": len(candidates), "strategy": "sql", "source": "demo"}
        candidates = vector_search_demo(f"{name} {address}".strip(), k)
        return {"candidates": candidates, "count": len(candidates), "strategy": "vector", "source": "demo"}

    norm_name = name.strip().title()
    norm_address = address.strip().title()
    strategy = "sql" if _is_abnormal(name) else "vector"

    if strategy == "vector":
        result = _vector_search_local(norm_name, norm_address, email, k, tenant_id, ws)
    else:
        result = _sql_search_local(norm_name, norm_address, ws)

    return {**result, "strategy": strategy, "normalized_name": norm_name, "normalized_address": norm_address}


def _vector_search_local(name, address, email, k, tenant_id, ws):
    indexes = {
        "full":        os.environ.get("VS_INDEX_FULL", ""),
        "last_addr":   os.environ.get("VS_INDEX_LAST_ADDR", ""),
        "first_email": os.environ.get("VS_INDEX_FIRST_EMAIL", ""),
    }
    missing = [n for n, v in indexes.items() if not v]
    if missing:
        return {"error": f"Missing VS index env vars: {missing}", "candidates": [], "count": 0}

    parts = name.split()
    last_name = parts[-1] if parts else name
    first_name = parts[0] if len(parts) > 1 else name
    queries = {
        "full":        f"{name} {address}".strip(),
        "last_addr":   f"{last_name} {address}".strip(),
        "first_email": f"{first_name} {email}".strip(),
    }
    filters = {"tenant_id": tenant_id} if tenant_id else {}
    columns = ["account_id", "first_name", "last_name", "service_address_line1", "account_number", "score"]

    seen: dict[str, dict] = {}
    for perm, index_name in indexes.items():
        raw = ws.vector_search_indexes.query_index(
            index_name=index_name, columns=columns, query_text=queries[perm],
            num_results=k, filters_json=str(filters) if filters else None,
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


def _sql_search_local(name, address, ws):
    table = os.environ.get("UTILITY_ACCOUNT_TABLE", "")
    if not table:
        return {"error": "UTILITY_ACCOUNT_TABLE not configured", "candidates": [], "count": 0}

    tokens = [t.strip(".,") for t in name.split() if len(t.strip(".,")) > 1]
    name_conditions = " AND ".join(f"name ILIKE '%{t}%'" for t in tokens)
    address_clause = f"AND address ILIKE '%{address.split()[0]}%'" if address else ""
    sql = f"SELECT account_id, name, address FROM {table} WHERE {name_conditions} {address_clause} LIMIT 20"

    def _warehouse_id(workspace):
        for wh in workspace.warehouses.list():
            if wh.warehouse_type and "serverless" in str(wh.warehouse_type).lower():
                return wh.id or ""
        for wh in workspace.warehouses.list():
            if wh.id:
                return wh.id
        raise RuntimeError("No SQL warehouse available")

    from databricks.sdk.service.sql import StatementState
    result = ws.statement_execution.execute_statement(
        warehouse_id=_warehouse_id(ws), statement=sql, wait_timeout="30s",
    )
    if result.status is None or result.status.state != StatementState.SUCCEEDED:
        return {"error": f"SQL failed: {result.status.error if result.status else 'unknown'}", "candidates": [], "count": 0}

    cols = [c.name for c in (result.manifest.schema.columns or [])]
    candidates = [dict(zip(cols, r)) for r in (result.result.data_array or [])]
    return {"candidates": candidates, "count": len(candidates), "source": "live"}
