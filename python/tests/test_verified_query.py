"""Phase 2 of golden/verified queries (#288): the verified_query tool matches a
user question to a golden query and runs its TRUSTED SQL with safe named-param
binding — the SQL is the verified template, param values are bound (never
concatenated), and a no-match falls back to author-your-own SQL.
"""
from __future__ import annotations

import pytest

import apx_agent._verified_query as vq
from apx_agent._verified_query import (
    _bind_params,
    _match_golden_query,
    _prepare_sql,
    run_verified_query,
)


GOLDEN = [
    {
        "question": "How many orders over $100?",
        "sql": "SELECT count(*) FROM {catalog}.{schema}.orders WHERE amount > @threshold",
        "params": [{"name": "threshold", "type": "int", "desc": "the dollar amount"}],
    },
    {
        "question": "Total amount by id",
        "sql": "SELECT id, sum(amount) FROM {catalog}.{schema}.orders GROUP BY id",
        "params": [],
    },
]


# ── matcher ──────────────────────────────────────────────────────────────────


def test_match_exact_and_paraphrase_above_threshold():
    assert _match_golden_query("how many orders over 100", GOLDEN) is GOLDEN[0]
    assert _match_golden_query("total amount by id", GOLDEN) is GOLDEN[1]


def test_match_below_threshold_and_empty_return_none():
    assert _match_golden_query("what is the weather today", GOLDEN) is None
    assert _match_golden_query("", GOLDEN) is None


# ── param binding (the safety boundary) ──────────────────────────────────────


def test_bind_declared_param_with_mapped_type():
    assert _bind_params(GOLDEN[0], {"threshold": "100"}) == [
        {"name": "threshold", "value": "100", "type": "INT"}
    ]


def test_bind_param_free_query_is_empty():
    assert _bind_params(GOLDEN[1], {}) == []


def test_bind_missing_param_raises():
    with pytest.raises(ValueError, match="threshold"):
        _bind_params(GOLDEN[0], {})


def test_bind_ignores_undeclared_extras():
    # Extras aren't in the SQL — binding them would error at the warehouse.
    assert _bind_params(GOLDEN[0], {"threshold": "5", "rogue": "x"}) == [
        {"name": "threshold", "value": "5", "type": "INT"}
    ]


# ── sql preparation ──────────────────────────────────────────────────────────


def test_prepare_sql_substitutes_config_and_converts_markers():
    out = _prepare_sql("SELECT * FROM {catalog}.{schema}.t WHERE x = @y", "cat", "sch")
    assert out == "SELECT * FROM cat.sch.t WHERE x = :y"


# ── run_verified_query: the end-to-end security property ─────────────────────


@pytest.mark.asyncio
async def test_runs_verified_template_with_bound_value_not_concatenated(monkeypatch):
    captured: dict = {}

    def fake_run_sql(ws, sql, *, warehouse_id=None, parameters=None):
        captured.update(sql=sql, warehouse_id=warehouse_id, parameters=parameters)
        return [{"count": 7}]

    monkeypatch.setattr(vq, "run_sql", fake_run_sql)
    out = await run_verified_query(
        "how many orders over 100", {"threshold": "100"}, object(),
        golden_queries=GOLDEN, catalog="cat", schema="sch", warehouse_id="wh-1",
    )
    assert out["matched"] is True and out["rows"] == [{"count": 7}]
    # The executed SQL is the VERIFIED template (config substituted, @→:), and the
    # user value is a BOUND parameter — never interpolated into the SQL string.
    assert captured["sql"] == "SELECT count(*) FROM cat.sch.orders WHERE amount > :threshold"
    assert captured["parameters"] == [{"name": "threshold", "value": "100", "type": "INT"}]
    assert "100" not in captured["sql"]  # injection-safety: value not in the SQL
    assert captured["warehouse_id"] == "wh-1"


@pytest.mark.asyncio
async def test_no_match_returns_false_without_touching_the_warehouse(monkeypatch):
    called: list = []
    monkeypatch.setattr(vq, "run_sql", lambda *a, **k: called.append(1) or [])
    out = await run_verified_query(
        "what is the weather today", {}, object(),
        golden_queries=GOLDEN, catalog="c", schema="s", warehouse_id=None,
    )
    assert out["matched"] is False
    assert called == []


@pytest.mark.asyncio
async def test_missing_param_errors_before_running(monkeypatch):
    called: list = []
    monkeypatch.setattr(vq, "run_sql", lambda *a, **k: called.append(1) or [])
    out = await run_verified_query(
        "how many orders over 100", {}, object(),
        golden_queries=GOLDEN, catalog="c", schema="s", warehouse_id=None,
    )
    assert out["matched"] is True and "threshold" in out["error"]
    assert called == []  # never reaches the warehouse with a missing param


# ── DataAgent wiring ─────────────────────────────────────────────────────────


_EXAMPLES_MD = """
# Examples

### How many orders over $100?
```sql
SELECT count(*) FROM {catalog}.{schema}.orders WHERE amount > @threshold
```
- @threshold (int): the dollar amount
"""


def _bundle(tmp_path, *, with_golden: bool):
    from apx_agent._okf import write_okf_bundle

    manifest = {"catalog": "c", "schema": "s", "tables": {"orders": ["id", "amount"]}}
    write_okf_bundle(manifest, tmp_path / "okf", timestamp="z")
    if with_golden:
        md = tmp_path / "okf" / "tables" / "orders.md"
        md.write_text(md.read_text().rstrip() + "\n\n" + _EXAMPLES_MD)
    return tmp_path / "okf"


def _tool_names(agent):
    return {getattr(t, "__name__", "") for t in agent._tool_fns}


def test_dataagent_attaches_verified_query_tool_when_golden_present(tmp_path):
    from apx_agent import DataAgent

    agent = DataAgent("c", "s", knowledge=str(_bundle(tmp_path, with_golden=True)))
    assert "verified_query" in _tool_names(agent)


def test_dataagent_accepts_explicit_verified_query_tool_name(tmp_path):
    from apx_agent import DataAgent

    agent = DataAgent(
        "c",
        "s",
        knowledge=str(_bundle(tmp_path, with_golden=True)),
        verified_query_tool_name="run_verified_sales_query",
    )
    assert "run_verified_sales_query" in _tool_names(agent)
    assert "verified_query" not in _tool_names(agent)


def test_dataagent_omits_verified_query_tool_without_golden(tmp_path):
    from apx_agent import DataAgent

    agent = DataAgent("c", "s", knowledge=str(_bundle(tmp_path, with_golden=False)))
    assert "verified_query" not in _tool_names(agent)
