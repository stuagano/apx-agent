"""verified_query — run a table's verified / "golden" SQL by NL-question match.

Phase 2 of golden queries (#288). Given the structured ``golden_queries`` an OKF
bundle carries (``{question, sql, params}`` — parsed in #289, rendered into the
prompt in #290), this exposes a tool that matches the user's question to a
golden query and runs its **trusted** SQL. The SQL is the verified template; the
LLM only supplies typed parameter VALUES, which are bound as Databricks named
parameters (never string-concatenated into SQL). So a hallucinated query can't
reach the warehouse — the worst an attacker-controlled param can do is a typed
bind value, exactly like a prepared statement.

Annotations are intentionally NOT deferred (no ``from __future__ import
annotations``) so ``UserClientDependency`` resolves eagerly at definition time,
matching ``sql_tools.py`` — see that module's note.
"""

import logging
import re
from typing import Any

from ._sql import run_sql

logger = logging.getLogger(__name__)

# Authored golden-query param types → Databricks SQL bind types.
_DBX_TYPE = {
    "int": "INT", "integer": "INT", "bigint": "BIGINT",
    "float": "FLOAT", "double": "DOUBLE", "decimal": "DECIMAL",
    "string": "STRING", "str": "STRING", "text": "STRING",
    "date": "DATE", "timestamp": "TIMESTAMP",
    "bool": "BOOLEAN", "boolean": "BOOLEAN",
}

# Min Jaccard token-overlap for a question to count as a match.
_MATCH_THRESHOLD = 0.6

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lowercase alphanumeric word tokens used for question matching."""
    return set(_WORD_RE.findall(text.lower()))


def _match_golden_query(question: str, golden_queries: "list[dict]") -> "dict | None":
    """Best golden query whose question token-overlaps ``question`` at or above
    the threshold, else ``None``.

    Jaccard over alphanumeric tokens — deterministic and offline (no embedding
    model for the MVP). Ties break to document order.
    """
    q_tokens = _tokens(question)
    if not q_tokens:
        return None
    best: "dict | None" = None
    best_score = 0.0
    for gq in golden_queries:
        gq_tokens = _tokens(gq.get("question") or "")
        if not gq_tokens:
            continue
        union = len(q_tokens | gq_tokens)
        score = len(q_tokens & gq_tokens) / union if union else 0.0
        if score > best_score:
            best_score, best = score, gq
    return best if best_score >= _MATCH_THRESHOLD else None


def _bind_params(golden_query: "dict", supplied: "dict") -> "list[dict]":
    """Build the Databricks bind-parameter list for a matched golden query.

    Pulls only the query's DECLARED params from ``supplied`` (extras are ignored
    — they aren't in the SQL, and passing unbound params errors). Raises
    ``ValueError`` naming any missing param. Values are bound as strings; the
    bind type comes from the declared type (default ``STRING``).
    """
    out: "list[dict]" = []
    for p in golden_query.get("params") or []:
        pname = p["name"]
        if pname not in supplied or supplied[pname] is None:
            raise ValueError(f"missing value for parameter '{pname}' ({p.get('desc', '')})".rstrip(" ()"))
        out.append({
            "name": pname,
            "value": str(supplied[pname]),
            "type": _DBX_TYPE.get((p.get("type") or "").lower().strip(), "STRING"),
        })
    return out


def _prepare_sql(sql: str, catalog: str, schema: str) -> str:
    """Substitute ``{catalog}``/``{schema}`` (config — NOT user input) and convert
    the authored ``@name`` parameter markers to Databricks ``:name`` markers."""
    sql = sql.replace("{catalog}", catalog).replace("{schema}", schema)
    return re.sub(r"@(\w+)", r":\1", sql)


async def run_verified_query(
    question: str,
    params: "dict | None",
    ws: Any,
    *,
    golden_queries: "list[dict]",
    catalog: str,
    schema: str,
    warehouse_id: "str | None",
    max_rows: int = 1000,
) -> "dict[str, Any]":
    """Match ``question`` to a golden query and run its trusted SQL.

    Testable core of :func:`verified_query_tool` (the tool closure just forwards
    here with the injected ``ws``). Returns ``matched: false`` when nothing
    matches so the agent can fall back to authoring SQL.
    """
    match = _match_golden_query(question, golden_queries)
    if match is None:
        return {"matched": False, "rows": [],
                "note": "No verified query matched; use the agent's SQL tool instead."}
    try:
        bind = _bind_params(match, params or {})
    except ValueError as e:
        return {"matched": True, "question": match.get("question"), "error": str(e), "rows": []}
    prepared = _prepare_sql(match["sql"], catalog, schema)
    try:
        rows = run_sql(ws, prepared, warehouse_id=warehouse_id, parameters=bind)
    except Exception as e:
        logger.warning("verified_query failed: %s", e)
        return {"matched": True, "question": match.get("question"), "error": str(e), "rows": []}
    truncated = len(rows) > max_rows
    return {
        "matched": True,
        "question": match.get("question"),
        "verified_sql": prepared,
        "row_count": len(rows),
        "truncated": truncated,
        "rows": rows[:max_rows],
    }


def verified_query_tool(
    golden_queries: "list[dict]",
    *,
    catalog: str,
    schema: str,
    warehouse_id: str | None = None,
    name: str = "verified_query",
    max_rows: int = 1000,
) -> Any:
    """Tool that runs a verified/golden SQL query matched from the user's question.

    The agent passes the user's ``question`` (verbatim or close) plus a ``params``
    object of any values the matched query needs; the tool finds the golden
    query, binds the params safely, and runs its trusted SQL.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    _desc = (
        "Run a VERIFIED, pre-approved SQL query for a known question. Pass the "
        "user's `question` and a `params` object with any values it needs (e.g. "
        '{"threshold": "100"}; pass {} if none). Prefer this over authoring SQL '
        "when the question matches one of the agent's known questions — the SQL "
        "is trusted and exact. Returns `matched: false` if no verified query "
        "fits; then use the agent's SQL tool."
    )

    async def _verified_query(
        question: str, params: dict, ws: UserClientDependency  # type: ignore[valid-type]
    ) -> dict[str, Any]:
        return await run_verified_query(
            question, params, ws,
            golden_queries=golden_queries, catalog=catalog, schema=schema,
            warehouse_id=warehouse_id, max_rows=max_rows,
        )

    return build_tool(
        _verified_query,
        name=name,
        description=_desc,
        resources=[ResourceSpec("sql_warehouse", warehouse_id)] if warehouse_id else (),
    )
