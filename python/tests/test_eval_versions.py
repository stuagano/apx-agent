"""Eval runs and judges record the pinned artifact versions they scored against (#1247)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apx_agent._agents import LlmAgent
from apx_agent._prompt_pins import PromptLoadError, PromptPinError, load_pinned_prompts, load_prompt_uri
from apx_agent._resolved_versions import ResolvedVersion, clear_resolved, resolved_versions, tag_run

mlflow = pytest.importorskip("mlflow")

_TOML = """\
schema_version = 1

[agent]
framework = "langgraph"
server = "agentbricks"

[experimental.prompts]
writer = "main.prompts.writer" # prompts:/main.prompts.writer/7
"""

_JUDGE_TEXT = "Is {{ outputs }} a good answer to {{ inputs }}? Answer yes or no."


class _Version:
    def __init__(self, template: str, version: int) -> None:
        self.template = template
        self.version = version


def _texts(uri: str) -> _Version:
    return _Version(f"text for {uri}", int(uri.rsplit("/", 1)[-1]))


def _judge_text(uri: str) -> _Version:
    return _Version(_JUDGE_TEXT, int(uri.rsplit("/", 1)[-1]))


@pytest.fixture(autouse=True)
def _clear():
    clear_resolved()
    yield
    clear_resolved()


@pytest.fixture
def local_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A throwaway sqlite tracking + registry store, restored afterwards."""
    tracking, registry = mlflow.get_tracking_uri(), mlflow.get_registry_uri()
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    mlflow.set_experiment("eval-versions")
    yield uri
    if mlflow.active_run() is not None:
        mlflow.end_run()
    mlflow.set_tracking_uri(tracking)
    mlflow.set_registry_uri(registry)


def _project(root: Path) -> Path:
    (root / "agent.toml").write_text(_TOML, encoding="utf-8")
    return root


def _links() -> MagicMock:
    return MagicMock(return_value=None)


# --- tag_run: the one place an eval run gets its versions -------------------


def test_tag_run_writes_apx_tags_and_mlflows_native_prompt_link(local_store: str) -> None:
    """Read the stored run back: apx tags plus MLflow's own linkedPrompts, from a real registry."""
    registered = mlflow.genai.register_prompt(name="writer", template="Write clearly.")
    uri = f"prompts:/writer/{registered.version}"
    version = ResolvedVersion("prompt", "writer", "writer", uri, str(registered.version))
    with mlflow.start_run() as run:
        tag_run(run.info.run_id, [version])
    tags = mlflow.MlflowClient().get_run(run.info.run_id).data.tags
    assert tags["apx.prompt.key"] == "writer"
    assert tags["apx.prompt.uri"] == uri
    assert tags["apx.prompt.version"] == str(registered.version)
    assert json.loads(tags["mlflow.linkedPrompts"]) == [{"name": "writer", "version": str(registered.version)}]


def test_tag_run_with_nothing_resolved_does_nothing() -> None:
    with patch("mlflow.MlflowClient") as client:
        tag_run("run-1", [])
    client.assert_not_called()


def test_judge_prompts_tag_separately_and_link_like_agent_prompts() -> None:
    agent_pin = ResolvedVersion("prompt", "writer", "main.prompts.writer", "prompts:/main.prompts.writer/7", "7")
    judge_pin = ResolvedVersion("judge_prompt", "tone", "main.judges.tone", "prompts:/main.judges.tone/3", "3")
    client = MagicMock()
    with patch("mlflow.MlflowClient", return_value=client):
        tag_run("run-1", [agent_pin, judge_pin])
    set_tags = {call.args[1]: call.args[2] for call in client.set_tag.call_args_list}
    assert set_tags == {
        "apx.prompt.key": "writer",
        "apx.prompt.uri": "prompts:/main.prompts.writer/7",
        "apx.prompt.version": "7",
        "apx.judge_prompt.key": "tone",
        "apx.judge_prompt.uri": "prompts:/main.judges.tone/3",
        "apx.judge_prompt.version": "3",
    }
    linked = [call.args for call in client.link_prompt_version_to_run.call_args_list]
    assert linked == [("run-1", "prompts:/main.prompts.writer/7"), ("run-1", "prompts:/main.judges.tone/3")]


# --- evaluate -----------------------------------------------------------------


def test_evaluate_records_the_pin_on_the_run_it_scored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_store: str
) -> None:
    from apx_agent._eval import evaluate

    monkeypatch.chdir(_project(tmp_path))
    monkeypatch.setattr("apx_agent._prompt_pins._default_loader", _texts)
    seen: dict[str, Any] = {}

    def fake_evaluate(**kwargs: Any) -> str:
        seen["run_id"] = mlflow.active_run().info.run_id
        return "ok"

    links = _links()
    with patch("apx_agent._eval.compile_to_chat_agent", return_value=MagicMock()), \
         patch("mlflow.genai.evaluate", fake_evaluate), \
         patch("mlflow.MlflowClient.link_prompt_version_to_run", links):
        assert evaluate(LlmAgent(instructions="local"), model="m", evalset=[{"request": "hi"}], scorers=[]) == "ok"
    assert mlflow.active_run() is None, "the run evaluate opened must be closed"
    tags = mlflow.MlflowClient().get_run(seen["run_id"]).data.tags
    assert tags["apx.prompt.uri"] == "prompts:/main.prompts.writer/7"
    assert tags["apx.prompt.version"] == "7"
    assert [call.args for call in links.call_args_list] == [(seen["run_id"], "prompts:/main.prompts.writer/7")]


def test_evaluate_reuses_an_active_run_and_leaves_it_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_store: str
) -> None:
    from apx_agent._eval import evaluate

    monkeypatch.chdir(_project(tmp_path))
    monkeypatch.setattr("apx_agent._prompt_pins._default_loader", _texts)
    with patch("apx_agent._eval.compile_to_chat_agent", return_value=MagicMock()), \
         patch("mlflow.genai.evaluate", MagicMock(return_value="ok")), \
         patch("mlflow.MlflowClient.link_prompt_version_to_run", _links()):
        with mlflow.start_run() as outer:
            evaluate(LlmAgent(instructions="local"), model="m", evalset=[{"request": "hi"}], scorers=[])
            assert mlflow.active_run().info.run_id == outer.info.run_id
    assert mlflow.MlflowClient().get_run(outer.info.run_id).data.tags["apx.prompt.version"] == "7"


def test_evaluate_without_pins_opens_no_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent._eval import evaluate

    monkeypatch.chdir(tmp_path)
    with patch("apx_agent._eval.compile_to_chat_agent", return_value=MagicMock()), \
         patch("mlflow.genai.evaluate", MagicMock(return_value="ok")), \
         patch("mlflow.start_run") as started:
        assert evaluate(LlmAgent(instructions="local"), model="m", evalset=[{"request": "hi"}], scorers=[]) == "ok"
    started.assert_not_called()


def test_a_failed_link_fails_the_eval_and_closes_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_store: str
) -> None:
    from apx_agent._eval import evaluate

    monkeypatch.chdir(_project(tmp_path))
    monkeypatch.setattr("apx_agent._prompt_pins._default_loader", _texts)
    fake_eval = MagicMock(return_value="ok")
    with patch("apx_agent._eval.compile_to_chat_agent", return_value=MagicMock()), \
         patch("mlflow.genai.evaluate", fake_eval), \
         patch("mlflow.MlflowClient.link_prompt_version_to_run", MagicMock(side_effect=PermissionError("denied"))):
        with pytest.raises(PermissionError, match="denied"):
            evaluate(LlmAgent(instructions="local"), model="m", evalset=[{"request": "hi"}], scorers=[])
    fake_eval.assert_not_called()
    assert mlflow.active_run() is None


# --- eval_against_endpoint ------------------------------------------------------


def test_eval_against_endpoint_loads_and_records_the_local_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_store: str
) -> None:
    from apx_agent._eval import eval_against_endpoint

    monkeypatch.chdir(_project(tmp_path))
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    loader = MagicMock(side_effect=_texts)
    monkeypatch.setattr("apx_agent._prompt_pins._default_loader", loader)
    seen: dict[str, Any] = {}

    def fake_evaluate(**kwargs: Any) -> str:
        seen["run_id"] = mlflow.active_run().info.run_id
        return "ok"

    with patch("mlflow.genai.evaluate", fake_evaluate), \
         patch("mlflow.MlflowClient.link_prompt_version_to_run", _links()):
        assert eval_against_endpoint("https://app.example", [{"inputs": {"question": "hi"}}], scorers=[]) == "ok"
    loader.assert_called_once_with("prompts:/main.prompts.writer/7")
    tags = mlflow.MlflowClient().get_run(seen["run_id"]).data.tags
    assert tags["apx.prompt.key"] == "writer"
    assert tags["apx.prompt.version"] == "7"


def test_eval_against_endpoint_fails_on_a_missing_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent._eval import eval_against_endpoint

    monkeypatch.chdir(_project(tmp_path))
    monkeypatch.setenv("DATABRICKS_TOKEN", "t")
    monkeypatch.setattr("apx_agent._prompt_pins._default_loader", MagicMock(side_effect=LookupError("no such prompt")))
    fake_eval = MagicMock()
    with patch("mlflow.genai.evaluate", fake_eval):
        with pytest.raises(PromptLoadError, match="prompts:/main.prompts.writer/7"):
            eval_against_endpoint("https://app.example", [], scorers=[])
    fake_eval.assert_not_called()


def test_load_pinned_prompts_replaces_what_an_earlier_agent_recorded(tmp_path: Path) -> None:
    _project(tmp_path)
    load_pinned_prompts(tmp_path, loader=_texts)
    assert [version.uri for version in resolved_versions()] == ["prompts:/main.prompts.writer/7"]
    bare = tmp_path / "bare"
    bare.mkdir()
    assert load_pinned_prompts(bare) == []
    assert resolved_versions() == []


# --- pinned_judge -----------------------------------------------------------------


def test_pinned_judge_uses_the_pinned_text_and_the_eval_run_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_store: str
) -> None:
    from apx_agent import pinned_judge
    from apx_agent._eval import evaluate

    monkeypatch.chdir(tmp_path)  # no agent.toml: only the judge is pinned
    judge = pinned_judge("tone", "prompts:/main.judges.tone/3", model="openai:/gpt-4.1-mini", loader=_judge_text)
    assert judge.name == "tone"
    assert judge.model == "openai:/gpt-4.1-mini"
    assert judge.instructions == _JUDGE_TEXT
    seen: dict[str, Any] = {}

    def fake_evaluate(**kwargs: Any) -> str:
        assert kwargs["scorers"] == [judge]
        seen["run_id"] = mlflow.active_run().info.run_id
        return "ok"

    links = _links()
    with patch("apx_agent._eval.compile_to_chat_agent", return_value=MagicMock()), \
         patch("mlflow.genai.evaluate", fake_evaluate), \
         patch("mlflow.MlflowClient.link_prompt_version_to_run", links):
        evaluate(LlmAgent(instructions="local"), model="m", evalset=[{"request": "hi"}], scorers=[judge])
    tags = mlflow.MlflowClient().get_run(seen["run_id"]).data.tags
    assert tags["apx.judge_prompt.key"] == "tone"
    assert tags["apx.judge_prompt.uri"] == "prompts:/main.judges.tone/3"
    assert tags["apx.judge_prompt.version"] == "3"
    assert "apx.prompt.key" not in tags
    assert [call.args[1] for call in links.call_args_list] == ["prompts:/main.judges.tone/3"]
    # A judge prompt never lands on agent traces.
    assert resolved_versions() == []


def test_pinned_judge_refuses_an_alias_or_unversioned_uri() -> None:
    from apx_agent import pinned_judge

    for uri in ("prompts:/main.judges.tone@production", "main.judges.tone", "prompts:/main.judges.tone/03"):
        with pytest.raises(PromptPinError):
            pinned_judge("tone", uri, model="openai:/gpt-4.1-mini", loader=_judge_text)


def test_load_prompt_uri_checks_the_loaded_version() -> None:
    with pytest.raises(PromptLoadError, match="loaded version 4, not 3"):
        load_prompt_uri("tone", "prompts:/main.judges.tone/3", loader=lambda uri: _Version("x", 4))
