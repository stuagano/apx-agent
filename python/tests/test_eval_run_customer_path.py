"""The documented evaluation path uses existing runtime behavior."""

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from apx_agent import Agent, SequentialAgent, _compile
from apx_agent.cli import main


@pytest.mark.parametrize("explicit,expected", [(None, "configured"), ("chosen", "chosen")])
def test_run_uses_project_model_unless_explicit(monkeypatch, tmp_path, explicit, expected):
    monkeypatch.setattr("apx_agent.cli._read_apx_agent_config", lambda path=None: {"model": "configured", "experiment": "experiment"})
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: Agent())
    calls = []

    def evaluate(agent, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(metrics={})

    monkeypatch.setattr("apx_agent.evaluate", evaluate)
    path = tmp_path / "cases.json"
    path.write_text('[{"inputs": {"question": "hello"}}]')
    args = ["eval", "run", str(path)]
    if explicit:
        args.extend(["--model", explicit])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert calls[0]["model"] == expected
    assert calls[0]["experiment"] == "experiment"


def test_endpoint_mode_does_not_inherit_local_model(monkeypatch, tmp_path):
    monkeypatch.setattr("apx_agent.cli._read_apx_agent_config", lambda path=None: {"model": "configured"})
    calls = []
    monkeypatch.setattr("apx_agent.eval_against_endpoint", lambda *a, **k: calls.append(a) or SimpleNamespace(metrics={}))
    path = tmp_path / "cases.json"
    path.write_text("[]")
    result = CliRunner().invoke(main, ["eval", "run", str(path), "--endpoint-url", "https://example.test", "--token", "fake"])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "https://example.test"


@pytest.mark.parametrize("actual,exit_code", [(2, 0), (99, 1)])
def test_fixture_run_reports_failed_step_using_real_compiler(monkeypatch, tmp_path, actual, exit_code):
    class Output(BaseModel):
        answer: int

    agent = SequentialAgent([Agent(name="first", output_schema=Output), Agent(name="second", output_schema=Output)], name="root")
    model = GenericFakeChatModel(messages=iter([AIMessage(content='{"answer":1}'), AIMessage(content=json.dumps({"answer": actual}))]))
    endpoints = []

    def build(endpoint, **kwargs):
        endpoints.append(endpoint)
        return model

    monkeypatch.setattr(_compile, "_build_chat_databricks", build)
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: agent)
    monkeypatch.setattr("apx_agent.cli._read_apx_agent_config", lambda path=None: {"model": "configured", "experiment": "must-not-be-used"})
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps([{"id": "example", "request": "go", "expected_outcome": "correct", "steps": [
        {"path": ["root", "first"], "tools": [], "expected_output": {"answer": 1}},
        {"path": ["root", "second"], "tools": [], "expected_output": {"answer": 2}},
    ]}]))
    result = CliRunner().invoke(main, ["eval", "run", str(path), "--fixtures"])
    assert result.exit_code == exit_code, result.output
    assert endpoints == ["configured", "configured"]
    assert f"example: {'correct' if actual == 2 else 'wrong'}" in result.output
    if actual != 2:
        assert "root > second" in result.output
        assert "Validated output did not match expected output" in result.output


@pytest.mark.parametrize("option", [
    ["--endpoint-url", "https://example.test"], ["--experiment", "exp"],
    ["--judge-model", "judge"], ["--user-token", "fake"], ["--token", "fake"],
    ["--profile", "profile"], ["--stream"], ["--no-stream"],
])
def test_fixture_mode_rejects_ignored_options_before_loading_agent(tmp_path, monkeypatch, option):
    def must_not_load(module):
        pytest.fail("Invalid options must be rejected before importing the agent")

    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", must_not_load)
    path = tmp_path / "fixtures.json"
    path.write_text("[]")
    result = CliRunner().invoke(main, ["eval", "run", str(path), "--fixtures", *option])
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_invalid_fixture_file_is_a_usage_error(monkeypatch, tmp_path):
    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: Agent())
    path = tmp_path / "fixtures.json"
    path.write_text("[]")
    result = CliRunner().invoke(main, ["eval", "run", str(path), "--fixtures", "--model", "fake"])
    assert result.exit_code == 2
    assert "Error:" in result.output


def test_documented_first_run_loads_the_real_project(monkeypatch, tmp_path):
    text = (Path(__file__).parents[2] / "docs/evaluate/overview.md").read_text()
    for language, name in [("python", "agent.py"), ("toml", "pyproject.toml"), ("json", "cases.json")]:
        block = re.search(rf"```{language}\n(.*?)```", text, re.S).group(1)
        (tmp_path / name).write_text(block)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    # Track the import slot so the example module cannot leak into later tests.
    monkeypatch.setitem(sys.modules, "agent", None)
    monkeypatch.delitem(sys.modules, "agent")
    calls = []

    def evaluate(agent, **kwargs):
        calls.append(kwargs)
        assert agent._instructions == "Answer arithmetic questions concisely."
        return SimpleNamespace(metrics={"correctness/mean": 1.0})

    monkeypatch.setattr("apx_agent.evaluate", evaluate)
    result = CliRunner().invoke(main, ["eval", "run", "cases.json", "--judge-model", "databricks"])
    assert result.exit_code == 0, result.output
    assert calls[0]["model"] == "your-model-endpoint"
    assert calls[0]["judge_model"] == "databricks"
    assert calls[0]["evalset"] == [{"inputs": {"question": "What is 2 + 2?"}, "expectations": {"expected_response": "4"}}]


@pytest.mark.parametrize("discovery", ["parent", "explicit-project"])
@pytest.mark.parametrize("override", [None, "local", "explicit"])
def test_run_model_follows_agent_project_discovery(monkeypatch, tmp_path, discovery, override):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text('[tool.apx.agent]\nname = "project-agent"\nmodel = "project-model"\n')
    (project / "review_agent.py").write_text('from apx_agent import Agent\nagent = Agent(instructions="Use the selected project.")\n')
    (project / "cases.json").write_text('[{"inputs": {"question": "hello"}}]')
    if override:
        (project / ".apx.local").write_text('[tool.apx.agent]\nmodel = "local-model"\n')
    cwd = project / "nested" if discovery == "parent" else tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("APX_PYPROJECT", raising=False)
    if discovery == "explicit-project":
        (cwd / "pyproject.toml").write_text('[tool.apx.agent]\nname = "other"\nmodel = "wrong-model"\n')
        monkeypatch.setenv("APX_PYPROJECT", str(project / "pyproject.toml"))
    monkeypatch.syspath_prepend(str(project))
    monkeypatch.setitem(sys.modules, "review_agent", None)
    monkeypatch.delitem(sys.modules, "review_agent")
    calls = []

    def evaluate(agent, **kwargs):
        assert agent._instructions == "Use the selected project."
        calls.append(kwargs)
        return SimpleNamespace(metrics={})

    monkeypatch.setattr("apx_agent.evaluate", evaluate)
    args = ["eval", "run", str(project / "cases.json"), "--module", "review_agent:agent"]
    if override == "explicit":
        args.extend(["--model", "explicit-model"])
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert calls[0]["model"] == (f"{override}-model" if override else "project-model")


def test_expected_escalation_passes_fixture_cli(monkeypatch, tmp_path):
    from apx_agent import ChainCaseResult, ChainEvalReport

    monkeypatch.setattr("apx_agent.cli._load_finalized_agent", lambda module: Agent())
    monkeypatch.setattr("apx_agent.evaluate_chain", lambda *a, **k: ChainEvalReport(
        cases=(ChainCaseResult(request="test", response="evidence", sub_agents_invoked=(), tool_calls=(),
                               case_id="source-down", outcome="escalated_with_evidence"),),
        outcome_counts={"correct": 0, "escalated_with_evidence": 1, "wrong": 0},
    ))
    path = tmp_path / "fixture.jsonl"
    path.write_text('{"id": "source-down"}\n')
    result = CliRunner().invoke(main, ["eval", "run", str(path), "--fixtures", "--model", "fake"])
    assert result.exit_code == 0, result.output
    assert "source-down: escalated_with_evidence" in result.output
