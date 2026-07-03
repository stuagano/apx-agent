"""Claim-vs-reality: example stores must not swallow infra errors into [] (#380).

LakebaseExampleStore.find_similar/.list and DeltaExampleStore.list wrapped their
query in `except Exception: return []`. So a transient outage, an expired OAuth
token, or a missing GRANT made the store silently report "no examples" — prompt
assembly then gets zero few-shot examples and the agent answers with degraded
quality, with no exception, no /readyz signal, and no trace of why. The sibling
memory store was deliberately hardened to let these propagate (H21).

This reconciles the *claim* ("here are the matching examples, or none") against
reality: an infra failure PROPAGATES (so it surfaces as a real error) rather than
masquerading as an empty result.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from apx_agent import DeltaExampleStore, LakebaseExampleStore
from apx_agent._example import ExampleFilter, FindSimilarOptions


def _embed(xs: list[str]) -> list[list[float]]:
    return [[0.0] for _ in xs]


@pytest.mark.unit
def test_delta_list_propagates_infra_error() -> None:
    def _boom(_sql: str) -> list[dict]:
        raise RuntimeError("warehouse unavailable")

    store = DeltaExampleStore(run_sql=_boom, auto_create=False)
    with pytest.raises(RuntimeError, match="warehouse unavailable"):
        store.list(ExampleFilter(agent_id="a"))


@pytest.mark.unit
def test_lakebase_list_propagates_infra_error() -> None:
    engine = MagicMock(name="engine")
    engine.connect.side_effect = RuntimeError("lakebase unreachable")
    store = LakebaseExampleStore(
        engine=engine, embedding_fn=_embed, embedding_dim=1, auto_create=False
    )
    with pytest.raises(RuntimeError, match="lakebase unreachable"):
        store.list(ExampleFilter(agent_id="a"))


@pytest.mark.unit
def test_lakebase_find_similar_propagates_infra_error() -> None:
    engine = MagicMock(name="engine")
    engine.connect.side_effect = RuntimeError("lakebase unreachable")
    store = LakebaseExampleStore(
        engine=engine, embedding_fn=_embed, embedding_dim=1, auto_create=False
    )
    with pytest.raises(RuntimeError, match="lakebase unreachable"):
        store.find_similar(FindSimilarOptions(agent_id="a", query="hi"))
