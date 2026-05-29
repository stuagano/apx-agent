"""Tests for _eval_chain.py — evaluate a chain + correlate sub-agents.

Covers:
  - evaluate_chain calls evaluate() with the same kwargs forwarded
  - Sub-agent invocations + tool calls are pulled off trace spans
  - Sub-agent coverage aggregates across cases
  - Cases without matching traces are dropped (not crashed)
  - Eval-set rows are read out of various shapes
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("mlflow")
pytest.importorskip("langgraph")

from apx_agent import Agent, evaluate_chain  # noqa: E402
from apx_agent._eval_chain import _request_texts_from_evalset  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _plain_tool(x: str) -> str:
    return x


def _fake_trace(
    *,
    request_text: str,
    sub_agent_endpoints: list[str] | None = None,
    tool_names: list[str] | None = None,
    duration_ms: int = 100,
    trace_id: str = "tr-1",
) -> SimpleNamespace:
    """Build a fake mlflow Trace whose root span carries the request text
    and child spans carry sub-agent / tool attributes."""
    root_span = SimpleNamespace(
        attributes={"apx.operation": "predict"},
        inputs={"messages": [{"role": "user", "content": request_text}]},
        outputs={"messages": [{"role": "assistant", "content": "supervisor reply"}]},
    )
    child_spans = []
    for endpoint in (sub_agent_endpoints or []):
        child_spans.append(SimpleNamespace(
            attributes={"apx.subagent.endpoint": endpoint},
            inputs=None,
            outputs=None,
        ))
    for tool_name in (tool_names or []):
        child_spans.append(SimpleNamespace(
            attributes={"apx.tool.name": tool_name},
            inputs=None,
            outputs=None,
        ))
    return SimpleNamespace(
        info=SimpleNamespace(
            trace_id=trace_id,
            execution_time_ms=duration_ms,
            tags={},
        ),
        data=SimpleNamespace(spans=[root_span, *child_spans]),
    )


# ---------------------------------------------------------------------------
# _request_texts_from_evalset
# ---------------------------------------------------------------------------


def test_request_texts_from_list_of_dicts() -> None:
    rows = [{"request": "q1"}, {"input": "q2"}, {"prompt": "q3"}]
    assert _request_texts_from_evalset(rows) == ["q1", "q2", "q3"]


def test_request_texts_from_list_of_strings() -> None:
    assert _request_texts_from_evalset(["q1", "q2"]) == ["q1", "q2"]


def test_request_texts_from_dataframe_like() -> None:
    df = SimpleNamespace(to_dict=lambda orient: [{"query": "q1"}, {"question": "q2"}])
    assert _request_texts_from_evalset(df) == ["q1", "q2"]


# ---------------------------------------------------------------------------
# evaluate_chain
# ---------------------------------------------------------------------------


def test_evaluate_chain_pairs_traces_to_evalset_by_request_text() -> None:
    agent = Agent(tools=[_plain_tool])

    traces = [
        _fake_trace(
            request_text="what is the lineage of orders?",
            sub_agent_endpoints=["data-triage"],
            tool_names=["classify_intent", "get_lineage"],
            trace_id="tr-1",
        ),
        _fake_trace(
            request_text="refund my last invoice",
            sub_agent_endpoints=["billing-specialist"],
            tool_names=["classify_intent", "get_recent_orders"],
            trace_id="tr-2",
        ),
    ]

    evalset = [
        {"request": "what is the lineage of orders?"},
        {"request": "refund my last invoice"},
    ]

    fake_evaluate = MagicMock(return_value=SimpleNamespace(metrics={}))

    with patch("apx_agent._eval_chain.evaluate", fake_evaluate), \
         patch("mlflow.search_traces", return_value=traces):
        report = evaluate_chain(
            agent,
            model="databricks-claude-sonnet-4-6",
            evalset=evalset,
            experiment="/Users/me/agents/triage",
        )

    # Two cases matched
    assert len(report.cases) == 2

    lineage_case = next(c for c in report.cases if "lineage" in c.request)
    refund_case = next(c for c in report.cases if "refund" in c.request)
    assert lineage_case.sub_agents_invoked == ("data-triage",)
    assert "get_lineage" in lineage_case.tool_calls
    assert refund_case.sub_agents_invoked == ("billing-specialist",)
    assert lineage_case.trace_id == "tr-1"

    # evaluate was called with forwarded kwargs
    assert fake_evaluate.call_args.kwargs["experiment"] == "/Users/me/agents/triage"


def test_evaluate_chain_aggregates_sub_agent_coverage() -> None:
    agent = Agent(tools=[_plain_tool])

    traces = [
        _fake_trace(request_text="q1", sub_agent_endpoints=["billing"], trace_id="t-1"),
        _fake_trace(request_text="q2", sub_agent_endpoints=["billing"], trace_id="t-2"),
        _fake_trace(request_text="q3", sub_agent_endpoints=["technical"], trace_id="t-3"),
    ]
    evalset = [{"request": "q1"}, {"request": "q2"}, {"request": "q3"}]

    with patch("apx_agent._eval_chain.evaluate", return_value=None), \
         patch("mlflow.search_traces", return_value=traces):
        report = evaluate_chain(
            agent,
            model="m",
            evalset=evalset,
            experiment="/x",
        )

    assert report.sub_agent_coverage == {"billing": 2, "technical": 1}


def test_evaluate_chain_drops_cases_without_matching_trace() -> None:
    agent = Agent(tools=[_plain_tool])

    traces = [
        _fake_trace(request_text="q1", trace_id="t-1"),
        # q2 has no matching trace
    ]
    evalset = [{"request": "q1"}, {"request": "q2"}]

    with patch("apx_agent._eval_chain.evaluate", return_value=None), \
         patch("mlflow.search_traces", return_value=traces):
        report = evaluate_chain(
            agent, model="m", evalset=evalset, experiment="/x",
        )

    assert len(report.cases) == 1
    assert report.cases[0].request == "q1"


def test_evaluate_chain_returns_empty_when_search_traces_fails() -> None:
    agent = Agent(tools=[_plain_tool])

    # The trace-correlation read needs span data (sub-agent / tool attributes
    # live on child spans), so a read failure must NOT be swallowed into an
    # empty coverage report — that reads as "no sub-agents fired" and silently
    # produces a wrong report. Surface it as a RuntimeError naming the
    # experiment and chaining the original cause.
    with patch("apx_agent._eval_chain.evaluate", return_value=None), \
         patch("mlflow.search_traces", side_effect=RuntimeError("backend down")):
        with pytest.raises(RuntimeError, match="search_traces failed") as exc_info:
            evaluate_chain(
                agent, model="m", evalset=[{"request": "q"}], experiment="/x",
            )

    assert "/x" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "backend down" in str(exc_info.value.__cause__)


def test_evaluate_chain_forwards_user_token_to_evaluate() -> None:
    agent = Agent(tools=[_plain_tool])
    fake_evaluate = MagicMock(return_value=None)

    with patch("apx_agent._eval_chain.evaluate", fake_evaluate), \
         patch("mlflow.search_traces", return_value=[]):
        evaluate_chain(
            agent,
            model="m",
            evalset=[],
            experiment="/x",
            user_token="obo-xyz",
            workspace_host="https://workspace.cloud.databricks.com",
        )

    assert fake_evaluate.call_args.kwargs["user_token"] == "obo-xyz"
    assert fake_evaluate.call_args.kwargs["workspace_host"] == "https://workspace.cloud.databricks.com"
