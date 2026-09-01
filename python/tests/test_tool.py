"""Tests for _tool.py — the @tool decorator and ToolMetadata.

Covers the design rules locked at the framework level:

  1. Bare ``@tool`` is sugar — attaches metadata but doesn't change runtime
     behavior. Function is callable identically.
  2. ``@tool(uc=...)`` requires a fully-qualified three-part UC name.
  3. ``@tool(uc=...)`` is rejected on functions with ``Dependencies.*``
     parameters (UC functions can't host user-scoped WorkspaceClient).
  4. ``@tool(grant=...)`` requires ``uc=...`` — grants without sync are
     meaningless.
  5. UC-marked tools auto-declare a ``ResourceSpec("uc_function", ...)``
     so log_agent picks them up without manual resource declaration.
  6. ``name`` and ``description`` overrides land on ``__name__`` /
     ``__doc__`` and on the metadata.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends

from apx_agent import (
    ResourceSpec,
    ToolMetadata,
    get_tool_metadata,
    tool,
)
from apx_agent._resources import get_resources


# A FastAPI-style Dependencies marker so we can exercise rule (3) without
# pulling in the full databricks SDK. The _inspect_tool_fn helper checks
# for fastapi.params.Depends regardless of what the dependency resolves to.
def _fake_dep() -> str:
    return "fake"


FakeDep = Annotated[str, Depends(_fake_dep)]


# ---------------------------------------------------------------------------
# Rule 1 — bare @tool
# ---------------------------------------------------------------------------


def test_bare_tool_attaches_metadata_and_preserves_call() -> None:
    @tool
    def upper(s: str) -> str:
        """Uppercase a string."""
        return s.upper()

    meta = get_tool_metadata(upper)
    assert isinstance(meta, ToolMetadata)
    assert meta.uc_name is None
    assert meta.grants == ()
    assert upper("hi") == "HI"


def test_bare_tool_does_not_attach_uc_resource() -> None:
    @tool
    def upper(s: str) -> str:
        """Uppercase a string."""
        return s.upper()

    assert get_resources(upper) == []


def test_tool_records_appkit_effect() -> None:
    @tool(effect="read")
    def lookup(value: str) -> str:
        return value

    assert get_tool_metadata(lookup).effect == "read"


def test_tool_without_effect_keeps_metadata_unspecified() -> None:
    @tool
    def apply(value: str) -> str:
        return value

    assert get_tool_metadata(apply).effect is None


@pytest.mark.parametrize("effect", ["invalid", "delete", "READ"])
def test_tool_rejects_invalid_effect(effect: str) -> None:
    with pytest.raises(ValueError, match="read.*write.*update.*destructive"):

        @tool(effect=effect)  # type: ignore[arg-type]
        def fn(value: str) -> str:
            return value


# ---------------------------------------------------------------------------
# Rule 2 — UC name validation
# ---------------------------------------------------------------------------


def test_uc_requires_three_parts() -> None:
    with pytest.raises(ValueError, match="three-part UC name"):

        @tool(uc="too.short")
        def fn(x: str) -> str:
            """doc"""
            return x


def test_uc_rejects_more_than_three_parts() -> None:
    with pytest.raises(ValueError, match="three-part UC name"):

        @tool(uc="a.b.c.d")
        def fn(x: str) -> str:
            """doc"""
            return x


def test_uc_rejects_empty_segment() -> None:
    with pytest.raises(ValueError, match="empty segment"):

        @tool(uc="main..fn")
        def fn(x: str) -> str:
            """doc"""
            return x


# ---------------------------------------------------------------------------
# Rule 3 — UC-syncable iff pure
# ---------------------------------------------------------------------------


def test_uc_rejects_function_with_dependencies() -> None:
    with pytest.raises(ValueError, match="Dependencies"):

        @tool(uc="main.tools.needs_ws")
        def needs_ws(x: str, dep: FakeDep) -> str:  # type: ignore[valid-type]
            """doc"""
            return x


def test_uc_allowed_on_pure_function() -> None:
    @tool(uc="main.tools.pure")
    def pure(x: str) -> str:
        """pure"""
        return x

    meta = get_tool_metadata(pure)
    assert meta is not None
    assert meta.uc_name == "main.tools.pure"


# ---------------------------------------------------------------------------
# Rule 4 — grants require uc
# ---------------------------------------------------------------------------


def test_grant_without_uc_raises() -> None:
    with pytest.raises(ValueError, match="grants only apply when"):

        @tool(grant=["agent_consumers"])
        def fn(x: str) -> str:
            """doc"""
            return x


def test_grant_with_uc_attaches() -> None:
    @tool(uc="main.tools.classify", grant=["agent_consumers", "data_team"])
    def classify(query: str) -> str:
        """Classify."""
        return query

    meta = get_tool_metadata(classify)
    assert meta is not None
    assert meta.grants == ("agent_consumers", "data_team")


# ---------------------------------------------------------------------------
# Rule 5 — auto resource declaration
# ---------------------------------------------------------------------------


def test_uc_tool_attaches_uc_function_resource() -> None:
    @tool(uc="main.tools.classify_intent")
    def classify_intent(query: str) -> str:
        """doc"""
        return query

    specs = get_resources(classify_intent)
    assert ResourceSpec("uc_function", "main.tools.classify_intent") in specs


# ---------------------------------------------------------------------------
# Rule 6 — name / description overrides
# ---------------------------------------------------------------------------


def test_name_override_updates_function_name() -> None:
    @tool(name="renamed")
    def original(x: str) -> str:
        """doc"""
        return x

    assert original.__name__ == "renamed"
    meta = get_tool_metadata(original)
    assert meta is not None
    assert meta.name_override == "renamed"


def test_description_override_updates_docstring_and_metadata() -> None:
    @tool(description="Overridden description.")
    def fn(x: str) -> str:
        """original docstring"""
        return x

    assert fn.__doc__ == "Overridden description."
    meta = get_tool_metadata(fn)
    assert meta is not None
    assert meta.description_override == "Overridden description."


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def test_decorator_rejects_non_callable() -> None:
    deco = tool(uc="main.tools.fn")
    with pytest.raises(TypeError):
        deco("not a function")  # type: ignore[arg-type]


def test_get_tool_metadata_returns_none_when_unmarked() -> None:
    def plain(x: str) -> str:
        """plain"""
        return x

    assert get_tool_metadata(plain) is None


def test_metadata_is_frozen() -> None:
    @tool(uc="main.tools.f")
    def fn(x: str) -> str:
        """doc"""
        return x

    meta = get_tool_metadata(fn)
    assert meta is not None
    with pytest.raises(Exception):  # dataclass frozen → FrozenInstanceError
        meta.uc_name = "different"  # type: ignore[misc]
