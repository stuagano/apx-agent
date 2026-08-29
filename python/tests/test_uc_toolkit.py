"""Tests for uc_function_toolkit and uc_function_tool's ws=... enhancement.

Covers:
  1. uc_function_tool with ws=... eagerly fetches the UC comment as the
     default description.
  2. uc_function_tool with ws=... but no comment uses the generic fallback.
  3. uc_function_tool with ws=... falls back gracefully when the get fails.
  4. uc_function_tool without ws=... still uses the generic fallback
     (existing behavior).
  5. uc_function_toolkit rejects bad catalog_schema arguments.
  6. uc_function_toolkit enumerates a schema and returns one tool per
     function, in iteration order.
  7. include / exclude filters work.
  8. A list failure returns an empty list (logged warning).
  9. Each emitted tool carries the function's UC comment as its docstring
     and a DatabricksFunction resource declaration.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import ResourceSpec, uc_function_tool, uc_function_toolkit
from apx_agent._resources import get_resources


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ws(
    *,
    functions_in_schema: list[Any] | None = None,
    function_info_by_name: dict[str, Any] | None = None,
    list_raises: Exception | None = None,
    get_raises: Exception | None = None,
) -> Any:
    """Build a MagicMock workspace client with the bits we exercise."""
    ws = MagicMock(name="WorkspaceClient")

    if list_raises is not None:
        ws.functions.list = MagicMock(side_effect=list_raises)
    else:
        ws.functions.list = MagicMock(return_value=iter(functions_in_schema or []))

    if get_raises is not None:
        ws.functions.get = MagicMock(side_effect=get_raises)
    elif function_info_by_name is not None:
        def _get(full_name: str) -> Any:
            return function_info_by_name[full_name]
        ws.functions.get = MagicMock(side_effect=_get)
    return ws


# ---------------------------------------------------------------------------
# uc_function_tool with ws=...
# ---------------------------------------------------------------------------


def test_uc_function_tool_uses_comment_when_ws_passed() -> None:
    ws = _make_ws(function_info_by_name={
        "main.tools.classify_intent": SimpleNamespace(
            comment="Classify a customer query as billing/technical/account/other.",
        ),
    })

    tool_fn = uc_function_tool("main.tools.classify_intent", ws=ws)

    assert tool_fn.__doc__ == "Classify a customer query as billing/technical/account/other."


def test_uc_function_tool_uses_explicit_description_over_comment() -> None:
    ws = _make_ws(function_info_by_name={
        "main.tools.fn": SimpleNamespace(comment="auto-fetched comment"),
    })
    tool_fn = uc_function_tool(
        "main.tools.fn",
        ws=ws,
        description="Explicit description wins.",
    )
    assert tool_fn.__doc__ == "Explicit description wins."


def test_uc_function_tool_falls_back_when_comment_empty() -> None:
    ws = _make_ws(function_info_by_name={
        "main.tools.fn": SimpleNamespace(comment=None),
    })
    tool_fn = uc_function_tool("main.tools.fn", ws=ws)
    assert tool_fn.__doc__ is not None
    assert "Execute the Unity Catalog function" in tool_fn.__doc__


def test_uc_function_tool_falls_back_when_fetch_fails() -> None:
    ws = _make_ws(get_raises=RuntimeError("no UC access"))
    tool_fn = uc_function_tool("main.tools.fn", ws=ws)
    assert tool_fn.__doc__ is not None
    assert "Execute the Unity Catalog function" in tool_fn.__doc__


def test_uc_function_tool_without_ws_uses_generic_description() -> None:
    tool_fn = uc_function_tool("main.tools.fn")
    assert tool_fn.__doc__ is not None
    assert "Execute the Unity Catalog function" in tool_fn.__doc__


# ---------------------------------------------------------------------------
# uc_function_toolkit — argument validation
# ---------------------------------------------------------------------------


def test_toolkit_rejects_non_two_part_identifier() -> None:
    with pytest.raises(ValueError, match="two-part catalog.schema"):
        uc_function_toolkit("not_two_parts", ws=MagicMock())


def test_toolkit_rejects_three_part_identifier() -> None:
    with pytest.raises(ValueError, match="two-part catalog.schema"):
        uc_function_toolkit("a.b.c", ws=MagicMock())


# ---------------------------------------------------------------------------
# uc_function_toolkit — enumeration
# ---------------------------------------------------------------------------


def _fn_info(name: str, comment: str | None = None) -> Any:
    return SimpleNamespace(
        name=name,
        full_name=f"main.tools.{name}",
        comment=comment,
    )


def test_toolkit_enumerates_all_functions() -> None:
    ws = _make_ws(functions_in_schema=[
        _fn_info("classify_intent", "Classify a customer query."),
        _fn_info("score_customer", "Compute a customer score."),
        _fn_info("format_address"),  # no comment
    ])

    tools = uc_function_toolkit("main.tools", ws=ws)

    assert len(tools) == 3
    # Names match short function names (uc_function_tool default)
    assert tools[0].__name__ == "classify_intent"
    assert tools[1].__name__ == "score_customer"
    assert tools[2].__name__ == "format_address"


def test_toolkit_enumerates_baked_functions_without_runtime_metadata(
    tmp_path, monkeypatch
) -> None:
    apx = tmp_path / ".apx"
    apx.mkdir()
    (apx / "schema.json").write_text(json.dumps({
        "catalog": "main",
        "schema": "tools",
        "tables": {},
        "functions": {
            "main.tools.classify_intent": {
                "comment": "Classify a customer request.",
                "data_type": "STRING",
                "parameters": [],
            },
            "other.tools.notify": {
                "comment": "Notify another system.",
                "data_type": "STRING",
                "parameters": [],
            },
        },
    }))
    monkeypatch.chdir(tmp_path)
    ws = _make_ws(list_raises=AssertionError("runtime metadata lookup"))

    tools = uc_function_toolkit("main.tools", ws=ws)

    assert [tool.__name__ for tool in tools] == ["classify_intent"]
    assert tools[0].__doc__ == "Classify a customer request."
    ws.functions.list.assert_not_called()


def test_toolkit_propagates_comments_as_descriptions() -> None:
    ws = _make_ws(functions_in_schema=[
        _fn_info("classify_intent", "Classify a customer query."),
        _fn_info("no_comment_fn"),
    ])

    tools = uc_function_toolkit("main.tools", ws=ws)

    assert tools[0].__doc__ == "Classify a customer query."
    # No comment → generic fallback
    assert tools[1].__doc__ is not None
    assert "Execute the Unity Catalog function" in tools[1].__doc__


def test_toolkit_attaches_uc_function_resource_per_tool() -> None:
    ws = _make_ws(functions_in_schema=[
        _fn_info("classify_intent"),
        _fn_info("score_customer"),
    ])

    tools = uc_function_toolkit("main.tools", ws=ws)

    assert ResourceSpec("uc_function", "main.tools.classify_intent") in get_resources(tools[0])
    assert ResourceSpec("uc_function", "main.tools.score_customer") in get_resources(tools[1])


# ---------------------------------------------------------------------------
# uc_function_toolkit — filters
# ---------------------------------------------------------------------------


def test_toolkit_include_whitelist() -> None:
    ws = _make_ws(functions_in_schema=[
        _fn_info("classify_intent"),
        _fn_info("score_customer"),
        _fn_info("format_address"),
    ])
    tools = uc_function_toolkit("main.tools", ws=ws, include=["classify_intent"])
    names = [t.__name__ for t in tools]
    assert names == ["classify_intent"]


def test_toolkit_exclude_blacklist() -> None:
    ws = _make_ws(functions_in_schema=[
        _fn_info("classify_intent"),
        _fn_info("score_customer"),
        _fn_info("internal_helper"),
    ])
    tools = uc_function_toolkit("main.tools", ws=ws, exclude=["internal_helper"])
    names = [t.__name__ for t in tools]
    assert names == ["classify_intent", "score_customer"]


def test_toolkit_include_and_exclude_compose() -> None:
    ws = _make_ws(functions_in_schema=[
        _fn_info("a"), _fn_info("b"), _fn_info("c"),
    ])
    tools = uc_function_toolkit(
        "main.tools", ws=ws,
        include=["a", "b", "c"],
        exclude=["b"],
    )
    assert [t.__name__ for t in tools] == ["a", "c"]


# ---------------------------------------------------------------------------
# uc_function_toolkit — failure modes
# ---------------------------------------------------------------------------


def test_toolkit_returns_empty_list_when_list_fails() -> None:
    ws = _make_ws(list_raises=RuntimeError("permission denied"))
    tools = uc_function_toolkit("main.tools", ws=ws)
    assert tools == []


def test_toolkit_skips_functions_with_no_name() -> None:
    ws = _make_ws(functions_in_schema=[
        _fn_info("classify_intent"),
        SimpleNamespace(name=None, full_name=None, comment=None),
        _fn_info("score_customer"),
    ])
    tools = uc_function_toolkit("main.tools", ws=ws)
    assert [t.__name__ for t in tools] == ["classify_intent", "score_customer"]


def test_toolkit_passes_catalog_schema_to_list_call() -> None:
    ws = _make_ws(functions_in_schema=[])
    uc_function_toolkit("main.agent_tools", ws=ws)
    ws.functions.list.assert_called_once_with(
        catalog_name="main", schema_name="agent_tools",
    )
