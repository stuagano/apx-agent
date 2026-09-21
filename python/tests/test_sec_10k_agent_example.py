"""Contract tests for the sec-10k-agent example (#665)."""

from __future__ import annotations

import os
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from apx_agent import SequentialAgent
from apx_agent._resources import collect_resource_specs

EXAMPLE_DIR = Path(__file__).parents[1] / "examples/sec-10k-agent"
AGENT_PY = EXAMPLE_DIR / "agent.py"


def _load_example():
    assert AGENT_PY.exists(), "sec-10k-agent example is missing"
    spec = spec_from_file_location("sec_10k_agent_example", AGENT_PY)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_sec_10k_agent_is_two_stage_sequential() -> None:
    example = _load_example()
    flow = example.create_sec_10k_agent("ka-from-test")
    assert isinstance(flow, SequentialAgent)
    stages = flow._agents
    assert [sub._name for sub in stages] == ["sec_10k_research", "sec_10k_brief"]
    research, brief = stages
    assert [fn.__name__ for fn in research._tool_fns] == ["ask_knowledge_assistant"]
    assert list(brief._tool_fns) == []


def test_collect_resource_specs_includes_factory_endpoint() -> None:
    example = _load_example()
    flow = example.create_sec_10k_agent("ka-from-test")
    endpoints = {
        spec.identifier
        for spec in collect_resource_specs(flow)
        if spec.kind == "serving_endpoint"
    }
    assert "ka-from-test" in endpoints


def test_blank_endpoint_raises_mentioning_env() -> None:
    example = _load_example()
    with pytest.raises(RuntimeError, match="APX_KA_ENDPOINT_NAME"):
        example.create_sec_10k_agent("")
    with pytest.raises(RuntimeError, match="APX_KA_ENDPOINT_NAME"):
        example.create_sec_10k_agent("   ")


def test_get_agent_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    example = _load_example()
    monkeypatch.delenv("APX_KA_ENDPOINT_NAME", raising=False)
    with pytest.raises(RuntimeError, match="APX_KA_ENDPOINT_NAME"):
        example.get_agent()

    monkeypatch.setenv("APX_KA_ENDPOINT_NAME", "ka-from-env")
    flow = example.get_agent()
    endpoints = {
        spec.identifier
        for spec in collect_resource_specs(flow)
        if spec.kind == "serving_endpoint"
    }
    assert "ka-from-env" in endpoints


def test_import_without_env_still_builds_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APX_KA_ENDPOINT_NAME", raising=False)
    example = _load_example()
    assert isinstance(example.agent, SequentialAgent)
    endpoints = {
        spec.identifier
        for spec in collect_resource_specs(example.agent)
        if spec.kind == "serving_endpoint"
    }
    assert "$APX_KA_ENDPOINT_NAME" in endpoints


def test_source_does_not_hardcode_a_demo_endpoint() -> None:
    source = AGENT_PY.read_text()
    forbidden = (
        "ka-10k",
        "ka-endpoint",
        "sec-10k-ka",
        "databricks-ka",
        "knowledge-assistant-10k",
    )
    lowered = source.lower()
    for name in forbidden:
        assert name not in lowered, f"example hardcodes demo endpoint {name!r}"
    assert "APX_KA_ENDPOINT_NAME" in source
