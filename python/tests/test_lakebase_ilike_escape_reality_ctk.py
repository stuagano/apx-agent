"""Claim-vs-reality: Lakebase conversation search must escape ILIKE wildcards.

The Lakebase store builds its search pattern as ``%{query}%`` and feeds it to
Postgres ``ILIKE`` without escaping the wildcard metacharacters ``%`` and ``_``
(and the ``\\`` escape char). The Delta store escapes them via ``_like_escape``
— so a search for the literal ``"50% off"`` returns wrong (over-broad) results
on Lakebase but correct results on Delta: backend-divergent semantics (#359).

A "the query returns rows" test can't see this. This reconciles the *claim*
("search matches the query text") against reality: the bind pattern the store
builds for ``"50% off"`` is run through a Postgres-ILIKE-semantics matcher and
must match ``"50% off here"`` but NOT ``"5000 dollars off"`` — which only holds
once ``%`` is escaped to a literal.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import LakebaseConversationStore


def _ilike_match(pattern: str, text: str) -> bool:
    """Minimal Postgres ILIKE (case-insensitive) semantics for one pattern.

    ``%`` = any run, ``_`` = one char, ``\\x`` = literal x (default ESCAPE).
    """
    out = ["(?is)^"]
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
        elif c == "%":
            out.append(".*")
            i += 1
        elif c == "_":
            out.append(".")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return re.match("".join(out), text) is not None


def _store() -> LakebaseConversationStore:
    # __init__ does no DB work; a mock engine is enough to build predicates.
    return LakebaseConversationStore(engine=MagicMock(name="engine"), auto_create=False)


def _pattern_for_list(query: str) -> str:
    params: dict[str, Any] = {}
    _store()._conv_list_predicates(None, None, query, params)
    return params["sq"]


def _pattern_for_search(query: str) -> str:
    store = _store()
    captured: dict[str, Any] = {}
    conn = MagicMock()

    def _execute(_sql: Any, params: Any) -> Any:
        captured.update(params)
        return MagicMock(mappings=lambda: [])

    conn.execute.side_effect = _execute
    store.engine.connect.return_value.__enter__.return_value = conn
    store.search(query)
    return captured["sq"]


@pytest.mark.unit
@pytest.mark.parametrize("pattern_for", [_pattern_for_list, _pattern_for_search])
def test_literal_percent_is_not_a_wildcard(pattern_for) -> None:
    pattern = pattern_for("50% off")
    assert _ilike_match(pattern, "50% off here"), f"literal match lost: {pattern!r}"
    assert not _ilike_match(pattern, "5000 dollars off"), (
        f"'%' acted as a wildcard — over-broad match: {pattern!r}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("pattern_for", [_pattern_for_list, _pattern_for_search])
def test_literal_underscore_is_not_a_wildcard(pattern_for) -> None:
    pattern = pattern_for("a_b")
    assert _ilike_match(pattern, "a_b"), f"literal underscore match lost: {pattern!r}"
    assert not _ilike_match(pattern, "axb"), f"'_' acted as a wildcard: {pattern!r}"
