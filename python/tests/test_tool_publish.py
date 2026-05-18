"""Tests for _tool_publish.py — publish_tools_to_uc.

Covers:

  1. Tree walking — discovers @tool(uc=...) in LlmAgent + SequentialAgent
     + nested compositions; ignores non-UC tools.
  2. Idempotent enumeration — same UC name registered twice yields one
     publish call.
  3. dry_run skips writes and returns descriptive results.
  4. Grants are applied per principal via the UC permissions API.
  5. A failure inside create_python_function surfaces as RuntimeError.
  6. A failure inside grant is logged-and-continues (function is published
     but the grant is dropped, so the result reflects that).

Uses a fake DatabricksFunctionClient + fake WorkspaceClient so the tests
run without unitycatalog-ai installed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import (
    Agent,
    PublishResult,
    SequentialAgent,
    publish_tools_to_uc,
    tool,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeUCClient:
    """Stub for ``unitycatalog.ai.core.databricks.DatabricksFunctionClient``."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_on = fail_on or set()

    def create_python_function(
        self,
        *,
        func: Any,
        catalog: str,
        schema: str,
        replace: bool = True,
    ) -> Any:
        full = f"{catalog}.{schema}.{func.__name__}"
        if full in self._fail_on:
            raise RuntimeError(f"simulated failure for {full}")
        self.calls.append({
            "catalog": catalog,
            "schema": schema,
            "function_name": func.__name__,
            "replace": replace,
        })


def _make_fake_ws(grant_failures: set[tuple[str, str]] | None = None) -> Any:
    """Return a MagicMock WorkspaceClient whose api_client.do records grants.

    ``grant_failures``: set of (uc_name, principal) tuples that should raise
    when the corresponding PATCH is attempted.
    """
    grant_failures = grant_failures or set()

    ws = MagicMock(name="WorkspaceClient")
    ws.api_client = MagicMock()

    def _do(method: str, path: str, body: Any = None) -> Any:
        assert method == "PATCH"
        assert path.startswith("/api/2.1/unity-catalog/permissions/function/")
        uc_name = path.rsplit("/", 1)[-1]
        changes = body["changes"]
        for change in changes:
            principal = change["principal"]
            if (uc_name, principal) in grant_failures:
                raise RuntimeError(f"simulated grant failure for {uc_name}/{principal}")
        return {}

    ws.api_client.do = MagicMock(side_effect=_do)
    return ws


# ---------------------------------------------------------------------------
# Tool fixtures
# ---------------------------------------------------------------------------


def _make_classify() -> Any:
    @tool(uc="main.tools.classify_intent", grant=["agent_consumers"])
    def classify_intent(query: str) -> str:
        """Classify a customer query."""
        return "billing" if "bill" in query.lower() else "other"
    return classify_intent


def _make_score() -> Any:
    @tool(uc="main.tools.score_customer", grant=["agent_consumers", "data_team"])
    def score_customer(customer_id: str) -> int:
        """Compute a customer score."""
        return 42
    return score_customer


def _plain_unmarked_tool(query: str) -> str:
    """A tool without @tool — should be ignored."""
    return query


# ---------------------------------------------------------------------------
# 1 — Tree walking
# ---------------------------------------------------------------------------


def test_walks_llm_agent_and_publishes_uc_tools() -> None:
    agent = Agent(tools=[_make_classify(), _make_score(), _plain_unmarked_tool])
    client = FakeUCClient()
    ws = _make_fake_ws()

    results = publish_tools_to_uc(agent, client=client, workspace_client=ws)

    assert len(results) == 2
    uc_names = {r.uc_name for r in results}
    assert uc_names == {"main.tools.classify_intent", "main.tools.score_customer"}
    assert len(client.calls) == 2


def test_walks_sequential_agent() -> None:
    a = Agent(tools=[_make_classify()])
    b = Agent(tools=[_make_score()])
    seq = SequentialAgent([a, b])
    client = FakeUCClient()
    ws = _make_fake_ws()

    results = publish_tools_to_uc(seq, client=client, workspace_client=ws)

    assert len(results) == 2


def test_no_uc_tools_returns_empty() -> None:
    agent = Agent(tools=[_plain_unmarked_tool])
    client = FakeUCClient()
    # ws not needed when no UC tools present
    results = publish_tools_to_uc(agent, client=client)
    assert results == []
    assert client.calls == []


# ---------------------------------------------------------------------------
# 2 — Idempotent enumeration
# ---------------------------------------------------------------------------


def test_duplicate_uc_name_only_publishes_once() -> None:
    classify = _make_classify()
    # Same callable registered in two agents — should publish once.
    a = Agent(tools=[classify])
    b = Agent(tools=[classify])
    seq = SequentialAgent([a, b])
    client = FakeUCClient()
    ws = _make_fake_ws()

    results = publish_tools_to_uc(seq, client=client, workspace_client=ws)

    assert len(results) == 1
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# 3 — dry_run
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write() -> None:
    agent = Agent(tools=[_make_classify()])
    client = FakeUCClient()
    ws = _make_fake_ws()

    results = publish_tools_to_uc(agent, client=client, workspace_client=ws, dry_run=True)

    assert len(results) == 1
    assert results[0].skipped is True
    assert results[0].skip_reason == "dry_run"
    assert client.calls == []
    # No grants attempted either
    ws.api_client.do.assert_not_called()


# ---------------------------------------------------------------------------
# 4 — Grants
# ---------------------------------------------------------------------------


def test_grants_applied_via_permissions_api() -> None:
    agent = Agent(tools=[_make_score()])  # two grants: agent_consumers + data_team
    client = FakeUCClient()
    ws = _make_fake_ws()

    results = publish_tools_to_uc(agent, client=client, workspace_client=ws)

    assert results[0].grants_applied == ("agent_consumers", "data_team")
    assert ws.api_client.do.call_count == 2
    # Both calls go to the score_customer function path
    for call in ws.api_client.do.call_args_list:
        method, path = call.args[0], call.args[1]
        assert method == "PATCH"
        assert path.endswith("score_customer")


def test_no_grants_means_no_permissions_api_calls() -> None:
    @tool(uc="main.tools.no_grant")
    def no_grant(x: str) -> str:
        """no grants"""
        return x

    agent = Agent(tools=[no_grant])
    client = FakeUCClient()
    # No grants → no need for a workspace client
    results = publish_tools_to_uc(agent, client=client)

    assert results[0].grants_applied == ()


# ---------------------------------------------------------------------------
# 5 — create_python_function failure surfaces as RuntimeError
# ---------------------------------------------------------------------------


def test_create_failure_raises_runtime_error() -> None:
    agent = Agent(tools=[_make_classify()])
    client = FakeUCClient(fail_on={"main.tools.classify_intent"})
    ws = _make_fake_ws()

    with pytest.raises(RuntimeError, match="Failed to publish"):
        publish_tools_to_uc(agent, client=client, workspace_client=ws)


# ---------------------------------------------------------------------------
# 6 — Grant failure is logged-and-continues
# ---------------------------------------------------------------------------


def test_grant_failure_does_not_abort_publish() -> None:
    agent = Agent(tools=[_make_score()])
    client = FakeUCClient()
    ws = _make_fake_ws(
        grant_failures={("main.tools.score_customer", "data_team")}
    )

    results = publish_tools_to_uc(agent, client=client, workspace_client=ws)

    assert len(results) == 1
    # agent_consumers succeeded; data_team failed and was dropped from the
    # applied list
    assert results[0].grants_applied == ("agent_consumers",)
    # Function publish itself succeeded
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


def test_publish_result_is_frozen() -> None:
    r = PublishResult(
        uc_name="main.x.y",
        function_name="y",
        grants_applied=("p",),
    )
    with pytest.raises(Exception):
        r.uc_name = "different"  # type: ignore[misc]
