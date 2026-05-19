"""Tests for ``_lint.py`` — static checks against agent definitions.

Covers each rule (L001-L004) individually and the public ``lint_agent``
entry point. Findings are sorted by (code, location) for stable diffs.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from apx_agent import (
    Agent,
    LlmAgent,
    LintFinding,
    SequentialAgent,
    Severity,
    lint_agent,
)
from apx_agent._lint import (
    _check_instructions,
    _check_model_name,
    _check_subagent_urls,
    _check_tool_docstrings,
    _iter_llm_agents,
)


# ---------------------------------------------------------------------------
# Tree walking
# ---------------------------------------------------------------------------


def test_iter_llm_agents_yields_leaf_for_plain_agent() -> None:
    agent = LlmAgent(tools=[], instructions="x")
    assert list(_iter_llm_agents(agent)) == [agent]


def test_iter_llm_agents_recurses_into_sequential() -> None:
    a = LlmAgent(tools=[], instructions="a")
    b = LlmAgent(tools=[], instructions="b")
    seq = SequentialAgent([a, b])
    assert list(_iter_llm_agents(seq)) == [a, b]


# ---------------------------------------------------------------------------
# L001 — missing instructions
# ---------------------------------------------------------------------------


def test_l001_flags_agent_with_no_instructions() -> None:
    agent = LlmAgent(tools=[], instructions="")
    findings = list(_check_instructions(agent))
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "L001"
    assert f.severity is Severity.ERROR
    assert "no instructions" in f.message.lower()


def test_l001_passes_agent_with_instructions() -> None:
    agent = LlmAgent(tools=[], instructions="You are a helpful assistant.")
    assert list(_check_instructions(agent)) == []


def test_l001_treats_whitespace_only_as_empty() -> None:
    agent = LlmAgent(tools=[], instructions="   \n  \t ")
    findings = list(_check_instructions(agent))
    assert len(findings) == 1


def test_l001_walks_composite_agents() -> None:
    """SequentialAgent with one well-formed child and one with no instructions."""
    good = LlmAgent(tools=[], instructions="good")
    bad = LlmAgent(tools=[], instructions="", name="bad")
    seq = SequentialAgent([good, bad])
    findings = list(_check_instructions(seq))
    assert len(findings) == 1
    assert "bad" in findings[0].location


# ---------------------------------------------------------------------------
# L002 — missing tool docstrings
# ---------------------------------------------------------------------------


def _well_documented_tool(x: int) -> int:
    """Square a number. The LLM can read this and know what it does."""
    return x * x


def _undocumented_tool(x: int) -> int:
    return x + 1


def test_l002_flags_tool_without_docstring() -> None:
    agent = LlmAgent(tools=[_undocumented_tool], instructions="x")
    findings = list(_check_tool_docstrings(agent))
    assert len(findings) == 1
    assert findings[0].code == "L002"
    assert findings[0].severity is Severity.WARNING
    assert "_undocumented_tool" in findings[0].location


def test_l002_passes_well_documented_tools() -> None:
    agent = LlmAgent(tools=[_well_documented_tool], instructions="x")
    assert list(_check_tool_docstrings(agent)) == []


def test_l002_finds_mixed_tools() -> None:
    agent = LlmAgent(
        tools=[_well_documented_tool, _undocumented_tool],
        instructions="x",
    )
    findings = list(_check_tool_docstrings(agent))
    assert len(findings) == 1
    assert "_undocumented_tool" in findings[0].location


# ---------------------------------------------------------------------------
# L003 — sub-agent URL env-var refs
# ---------------------------------------------------------------------------


def test_l003_flags_unset_env_var_in_subagent_url() -> None:
    agent = LlmAgent(
        tools=[],
        instructions="x",
        sub_agents=["$AGENT_THAT_DOES_NOT_EXIST_xyz123/api"],
    )
    findings = list(_check_subagent_urls(agent))
    assert len(findings) == 1
    assert findings[0].code == "L003"
    assert findings[0].severity is Severity.ERROR
    assert "AGENT_THAT_DOES_NOT_EXIST_xyz123" in findings[0].message


def test_l003_passes_when_env_var_is_set() -> None:
    agent = LlmAgent(
        tools=[],
        instructions="x",
        sub_agents=["$APX_LINT_TEST_VAR/api"],
    )
    with patch.dict(os.environ, {"APX_LINT_TEST_VAR": "https://example.com"}):
        findings = list(_check_subagent_urls(agent))
    assert findings == []


def test_l003_passes_literal_urls_without_env_vars() -> None:
    agent = LlmAgent(
        tools=[],
        instructions="x",
        sub_agents=["https://my-agent.databricksapps.com/api"],
    )
    assert list(_check_subagent_urls(agent)) == []


def test_l003_handles_curly_brace_form() -> None:
    """${VAR} syntax should also be recognized."""
    agent = LlmAgent(
        tools=[],
        instructions="x",
        sub_agents=["${UNSET_LINT_VAR_curly}/api"],
    )
    findings = list(_check_subagent_urls(agent))
    assert len(findings) == 1
    assert "UNSET_LINT_VAR_curly" in findings[0].message


# ---------------------------------------------------------------------------
# L004 — model name shape
# ---------------------------------------------------------------------------


def test_l004_passes_databricks_prefixed_model() -> None:
    agent = LlmAgent(tools=[], instructions="x")
    findings = list(_check_model_name(agent, model="databricks-claude-sonnet-4-6"))
    assert findings == []


def test_l004_passes_endpoints_prefixed_model() -> None:
    agent = LlmAgent(tools=[], instructions="x")
    findings = list(_check_model_name(agent, model="endpoints/billing-agent"))
    assert findings == []


def test_l004_flags_unknown_prefix_with_warning() -> None:
    agent = LlmAgent(tools=[], instructions="x")
    findings = list(_check_model_name(agent, model="gpt-4o-2024-08-06"))
    assert len(findings) == 1
    assert findings[0].code == "L004"
    assert findings[0].severity is Severity.WARNING
    assert "gpt-4o-2024-08-06" in findings[0].message


def test_l004_deduplicates_same_model_across_locations() -> None:
    """Same model from kwarg and from agent attribute → reported once."""
    agent = LlmAgent(tools=[], instructions="x")
    agent._model = "claude-3-5-sonnet-20241022"  # non-Databricks name
    findings = list(_check_model_name(agent, model="claude-3-5-sonnet-20241022"))
    assert len(findings) == 1


def test_l004_no_findings_when_no_model_passed_and_none_on_agent() -> None:
    agent = LlmAgent(tools=[], instructions="x")
    assert list(_check_model_name(agent, model=None)) == []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def test_lint_agent_returns_empty_for_clean_agent() -> None:
    agent = LlmAgent(
        tools=[_well_documented_tool],
        instructions="You are a helpful assistant.",
    )
    findings = lint_agent(agent, model="databricks-claude-sonnet-4-6")
    assert findings == []


def test_lint_agent_aggregates_multiple_findings() -> None:
    """All rules fire at once: missing instructions + undoc tool + unset env."""
    agent = LlmAgent(
        tools=[_undocumented_tool],
        instructions="",
        sub_agents=["$UNSET_LINT_ENV_abc/api"],
    )
    findings = lint_agent(agent, model="gpt-4o")

    codes = {f.code for f in findings}
    assert codes == {"L001", "L002", "L003", "L004"}


def test_lint_agent_findings_are_sorted_by_code_then_location() -> None:
    """Findings come back in stable order for diff-friendly CI output."""
    agent = LlmAgent(
        tools=[_undocumented_tool],
        instructions="",
        sub_agents=["$UNSET_LINT_ENV_xxx/a"],
    )
    findings = lint_agent(agent, model="gpt-4o")
    codes = [f.code for f in findings]
    assert codes == sorted(codes)


def test_lint_finding_is_error_helper() -> None:
    f = LintFinding(code="L001", severity=Severity.ERROR, location="x", message="y")
    assert f.is_error()
    w = LintFinding(code="L002", severity=Severity.WARNING, location="x", message="y")
    assert not w.is_error()
