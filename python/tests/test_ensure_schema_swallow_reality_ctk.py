"""Claim-vs-reality: ensure_schema must not swallow real DDL errors (#383).

ensure_schema wrapped the review/constraints/agent_evals CREATE TABLE statements
in `except Exception: pass` with the rationale "table likely already exists". But
`CREATE TABLE IF NOT EXISTS` never errors on existence — so the swallow only hid
REAL failures (permission denied, invalid catalog/namespace), which then
resurfaced later as an opaque "table or view not found" far from the cause, with
no signal that schema bootstrap never happened.

This reconciles the *claim* ("the schema is created (or already exists)") against
reality: a DDL error propagates instead of being silently discarded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apx_agent.workflow.population_store import PopulationStore


@pytest.mark.unit
def test_ensure_schema_propagates_ddl_errors() -> None:
    store = PopulationStore.__new__(PopulationStore)
    store.config = SimpleNamespace(  # type: ignore[attr-defined]
        population_table="cat.sch.population",
        table_namespace="cat.sch",
        review_table="cat.sch.review",
    )

    calls = {"n": 0}

    def _exec(_sql: str) -> list:
        calls["n"] += 1
        # First statement (the population table) succeeds; a later one — a
        # review/constraints/agent_evals DDL that used to be swallowed — fails.
        if calls["n"] >= 2:
            raise RuntimeError("permission denied to create table")
        return []

    store._sql_exec = _exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="permission denied"):
        store.ensure_schema()
