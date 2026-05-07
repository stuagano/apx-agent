"""Supervisor agent — normalizes AFR records and searches for candidates.

When SEARCH_SERVICE_URL is configured, the Supervisor calls the account-search-service
API via HTTP (recommended when running as separate deployed apps). Otherwise it runs
Vector Search + SQL locally — useful for single-app dev and DEMO_MODE.
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
    return bool(_INITIAL_RE.search(name) or _ACRONYM_RE.search(name))


def normalize_record(
    name: str,
    address: str = "",
    account_number: str = "",
    ws: Workspace = None,
) -> dict[str, Any]:
    """Normalize an AFR applicant record.

    Returns normalized fields plus 'strategy': 'vector' | 'sql'.
    name: applicant full name (raw, may have extra spaces or punctuation)
    address: service address (optional)
    account_number: utility account number (optional)"""
    return {
        "name": name.strip().title(),
        "address": address.strip().title(),
        "account_number": account_number.strip(),
        "strategy": "sql" if _is_abnormal(name) else "vector",
    }


def search_accounts(
    applicant_name: str,
    address: str = "",
    email: str = "",
    tenant_id: str = "",
    k: int = 10,
    ws: Workspace = None,
) -> dict[str, Any]:
    """Search utility accounts for AFR application candidates.

    If SEARCH_SERVICE_URL is set, calls the account-search-service API (recommended for
    separate-app deployments). Otherwise runs Vector Search + SQL locally.

    Fans out across embed_full (full name+address), embed_last_addr (familial match),
    and embed_first_email (maiden name) indexes, deduplicates by account_id.

    applicant_name: normalized full name
    address: normalized service address
    email: optional — used for maiden name matching via first+email index
    tenant_id: optional — restricts search to one utility tenant
    k: candidates per VS index before deduplication"""
    service_url = os.environ.get("SEARCH_SERVICE_URL", "").rstrip("/")
    if service_url:
        return _call_search_service(service_url, applicant_name, address, email, tenant_id, k, ws)
    return _search_local(applicant_name, address, email, tenant_id, k, ws)


def _call_search_service(url, applicant_name, address, email, tenant_id, k, ws):
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
            "applicant_name": applicant_name,
            "address": address,
            "email": email,
            "tenant_id": tenant_id,
            "k": k,
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def _search_local(applicant_name, address, email, tenant_id, k, ws):
    """Local search — same logic as account-search-service, embedded for single-app dev."""
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        from .demo_data import vector_search_demo, sql_search_demo
        if _is_abnormal(applicant_name):
            candidates = sql_search_demo(applicant_name, address)
            return {"candidates": candidates, "count": len(candidates), "source": "demo", "strategy": "sql"}
        candidates = vector_search_demo(f"{applicant_name} {address}".strip(), k)
        return {"candidates": candidates, "count": len(candidates), "source": "demo", "strategy": "vector"}

    indexes = {
        "full":        os.environ.get("VS_INDEX_FULL", ""),
        "last_addr":   os.environ.get("VS_INDEX_LAST_ADDR", ""),
        "first_email": os.environ.get("VS_INDEX_FIRST_EMAIL", ""),
    }
    missing = [n for n, v in indexes.items() if not v]
    if missing:
        return _sql_fallback(applicant_name, address, ws)

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
    return {"candidates": candidates, "count": len(candidates), "strategy": "vector"}


def _sql_fallback(name, address, ws):
    if os.environ.get("DEMO_MODE", "").lower() == "true":
        from .demo_data import sql_search_demo
        candidates = sql_search_demo(name, address)
        return {"candidates": candidates, "count": len(candidates), "source": "demo", "strategy": "sql"}

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
    return {"candidates": candidates, "count": len(candidates), "strategy": "sql"}


SUPERVISOR_INSTRUCTIONS = """
You are the Supervisor in an entity resolution system for utility company AFR (Affordable Rate) applications.

Your job:
1. Call normalize_record on the applicant's name, address, and account number.
2. Call search_accounts with the normalized name, address, email (if known), and tenant_id.
   This searches across all three VS indexes (full name+address, last name+address,
   first name+email) and returns deduplicated candidates ranked by similarity score.
3. Review the candidates. If count is 0, inform the evaluator — they may ask you to retry
   with different hints (e.g., try just the last name, or just the account number).
4. Hand off to the evaluator with a context summary: normalized applicant, candidates, count.

Do NOT make the enrollment decision yourself — that is the evaluator's role.
If the evaluator sends a retry request with search hints, apply the hints and call search_accounts again.
""".strip()

supervisor = LlmAgent(
    tools=[normalize_record, search_accounts],
    instructions=SUPERVISOR_INSTRUCTIONS,
    max_iterations=6,
)
