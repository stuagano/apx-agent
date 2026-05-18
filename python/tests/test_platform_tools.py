"""Tests for the platform tool factories — vector_search, sql, foundation_model.

Each factory:
  * returns a callable with __name__/__doc__ overrides honored
  * attaches the correct ResourceSpec via _apx_resources
  * executes against a FakeWorkspace stub and returns the expected shape
  * survives an SDK exception with a graceful fallback (logs + safe return)

Uses MagicMock workspace clients to avoid real Databricks dependencies.
"""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apx_agent import (
    ResourceSpec,
    foundation_model_tool,
    sql_tool,
    vector_search_tool,
)
from apx_agent._resources import get_resources


# ===========================================================================
# vector_search_tool
# ===========================================================================


def _fake_vs_response(rows: list[list[Any]], column_names: list[str]) -> Any:
    """Build a fake response shape matching ws.vector_search_indexes.query_index."""
    return SimpleNamespace(
        manifest=SimpleNamespace(columns=[SimpleNamespace(name=n) for n in column_names]),
        result=SimpleNamespace(data_array=rows),
    )


def test_vector_search_attaches_resource() -> None:
    tool_fn = vector_search_tool("main.search.docs_index")
    assert ResourceSpec("vector_search_index", "main.search.docs_index") in get_resources(tool_fn)


def test_vector_search_default_name_and_description() -> None:
    tool_fn = vector_search_tool("main.search.docs_index")
    assert tool_fn.__name__ == "vector_search"
    assert "main.search.docs_index" in (tool_fn.__doc__ or "")


def test_vector_search_custom_name_and_description() -> None:
    tool_fn = vector_search_tool(
        "main.search.docs_index",
        name="find_docs",
        description="Find relevant docs.",
    )
    assert tool_fn.__name__ == "find_docs"
    assert tool_fn.__doc__ == "Find relevant docs."


@pytest.mark.asyncio
async def test_vector_search_returns_dicts_keyed_by_columns() -> None:
    tool_fn = vector_search_tool("main.search.docs_index", num_results=2)

    fake_ws = MagicMock()
    fake_ws.vector_search_indexes.query_index.return_value = _fake_vs_response(
        rows=[["doc-1", "Title 1", 0.95], ["doc-2", "Title 2", 0.87]],
        column_names=["doc_id", "title", "score"],
    )

    result = await tool_fn(query="how do I deploy", ws=fake_ws)

    assert result == [
        {"doc_id": "doc-1", "title": "Title 1", "score": 0.95},
        {"doc_id": "doc-2", "title": "Title 2", "score": 0.87},
    ]
    fake_ws.vector_search_indexes.query_index.assert_called_once()
    call_kwargs = fake_ws.vector_search_indexes.query_index.call_args.kwargs
    assert call_kwargs["index_name"] == "main.search.docs_index"
    assert call_kwargs["query_text"] == "how do I deploy"
    assert call_kwargs["num_results"] == 2


@pytest.mark.asyncio
async def test_vector_search_returns_empty_on_exception() -> None:
    tool_fn = vector_search_tool("main.search.docs_index")
    fake_ws = MagicMock()
    fake_ws.vector_search_indexes.query_index.side_effect = RuntimeError("network down")
    result = await tool_fn(query="anything", ws=fake_ws)
    assert result == []


# ===========================================================================
# sql_tool
# ===========================================================================


def test_sql_tool_attaches_warehouse_resource_when_id_given() -> None:
    tool_fn = sql_tool(warehouse_id="wh-prod-123")
    assert ResourceSpec("sql_warehouse", "wh-prod-123") in get_resources(tool_fn)


def test_sql_tool_no_resource_without_warehouse_id() -> None:
    tool_fn = sql_tool()
    # No warehouse declared → no resource (auto-discovery at call time)
    assert get_resources(tool_fn) == []


def test_sql_tool_default_name() -> None:
    tool_fn = sql_tool()
    assert tool_fn.__name__ == "run_sql"


@pytest.mark.asyncio
async def test_sql_tool_returns_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_sql(ws: Any, sql: str, *, warehouse_id: Any = None, parameters: Any = None) -> list[dict[str, Any]]:
        captured["sql"] = sql
        captured["warehouse_id"] = warehouse_id
        return [{"col_a": 1, "col_b": "x"}, {"col_a": 2, "col_b": "y"}]

    monkeypatch.setattr("apx_agent.sql_tools.run_sql", fake_run_sql)

    tool_fn = sql_tool(warehouse_id="wh-1")
    fake_ws = MagicMock()
    result = await tool_fn(query="SELECT * FROM t", ws=fake_ws)

    assert result == {
        "row_count": 2,
        "truncated": False,
        "rows": [{"col_a": 1, "col_b": "x"}, {"col_a": 2, "col_b": "y"}],
    }
    assert captured["sql"] == "SELECT * FROM t"
    assert captured["warehouse_id"] == "wh-1"


@pytest.mark.asyncio
async def test_sql_tool_truncates_large_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_sql(ws: Any, sql: str, **_: Any) -> list[dict[str, Any]]:
        return [{"i": i} for i in range(2500)]

    monkeypatch.setattr("apx_agent.sql_tools.run_sql", fake_run_sql)
    tool_fn = sql_tool(warehouse_id="wh-1", max_rows=1000)
    result = await tool_fn(query="SELECT * FROM huge", ws=MagicMock())

    assert result["row_count"] == 1000
    assert result["truncated"] is True
    assert len(result["rows"]) == 1000


@pytest.mark.asyncio
async def test_sql_tool_returns_error_payload_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_sql(ws: Any, sql: str, **_: Any) -> list[dict[str, Any]]:
        raise RuntimeError("Query failed: syntax error")

    monkeypatch.setattr("apx_agent.sql_tools.run_sql", fake_run_sql)
    tool_fn = sql_tool(warehouse_id="wh-1")
    result = await tool_fn(query="BAD SQL", ws=MagicMock())

    assert "error" in result
    assert "syntax error" in result["error"]
    assert result["rows"] == []


# ===========================================================================
# foundation_model_tool
# ===========================================================================


def _fake_chat_response(text: str) -> Any:
    """Build a fake response shape matching ws.serving_endpoints.query."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def test_foundation_model_attaches_serving_endpoint_resource() -> None:
    tool_fn = foundation_model_tool("databricks-claude-opus-4-7")
    assert ResourceSpec("serving_endpoint", "databricks-claude-opus-4-7") in get_resources(tool_fn)


def test_foundation_model_default_name() -> None:
    tool_fn = foundation_model_tool("databricks-claude-opus-4-7")
    assert tool_fn.__name__ == "ask_model"


def test_foundation_model_custom_name_and_description() -> None:
    tool_fn = foundation_model_tool(
        "databricks-claude-opus-4-7",
        name="ask_opus",
        description="Ask the deep reasoning specialist.",
    )
    assert tool_fn.__name__ == "ask_opus"
    assert tool_fn.__doc__ == "Ask the deep reasoning specialist."


@pytest.mark.asyncio
async def test_foundation_model_returns_response_text() -> None:
    tool_fn = foundation_model_tool(
        "databricks-claude-opus-4-7",
        system_prompt="You are concise.",
        temperature=0.2,
        max_tokens=128,
    )

    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.return_value = _fake_chat_response("the answer is 42")

    result = await tool_fn(prompt="what's the answer?", ws=fake_ws)

    assert result == "the answer is 42"
    call_kwargs = fake_ws.serving_endpoints.query.call_args.kwargs
    assert call_kwargs["name"] == "databricks-claude-opus-4-7"
    assert call_kwargs["temperature"] == 0.2
    assert call_kwargs["max_tokens"] == 128
    # System prompt prepended; user prompt is the second message
    messages = call_kwargs["messages"]
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_foundation_model_no_system_prompt_sends_only_user() -> None:
    tool_fn = foundation_model_tool("databricks-claude-sonnet-4-6")

    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.return_value = _fake_chat_response("hi")

    await tool_fn(prompt="hello", ws=fake_ws)

    messages = fake_ws.serving_endpoints.query.call_args.kwargs["messages"]
    assert len(messages) == 1


@pytest.mark.asyncio
async def test_foundation_model_returns_error_string_on_exception() -> None:
    tool_fn = foundation_model_tool("databricks-claude-opus-4-7")

    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.side_effect = RuntimeError("endpoint unreachable")

    result = await tool_fn(prompt="anything", ws=fake_ws)

    assert "Model query failed" in result
    assert "endpoint unreachable" in result


@pytest.mark.asyncio
async def test_foundation_model_returns_empty_on_no_choices() -> None:
    tool_fn = foundation_model_tool("databricks-claude-opus-4-7")
    fake_ws = MagicMock()
    fake_ws.serving_endpoints.query.return_value = SimpleNamespace(choices=[])
    result = await tool_fn(prompt="anything", ws=fake_ws)
    assert result == ""
