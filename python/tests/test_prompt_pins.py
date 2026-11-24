"""Pinned Prompt Registry bindings: parse, load, doctor, traces, eval, promotion."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from ctk import Artifact, verify

from apx_agent._agents import LlmAgent
from apx_agent._doctor import Status
from apx_agent._prompt_pins import (
    PromptLoadError,
    PromptPinError,
    apply_pinned_prompts,
    bump_prompt_pin,
    check_prompt_pins,
    open_prompt_pin_pr,
    read_prompt_bindings,
)
from apx_agent._resolved_versions import clear_resolved

_HEADER = """\
schema_version = 1

[agent]
framework = "langgraph"
server = "agentbricks"

[experimental.prompts]
"""


class _Version:
    def __init__(self, template: str, version: int) -> None:
        self.template = template
        self.version = version


@pytest.fixture(autouse=True)
def _clear_prompt_tags():
    clear_resolved()
    yield
    clear_resolved()


def _write(root: Path, body: str) -> Path:
    path = root / "agent.toml"
    path.write_text(_HEADER + body, encoding="utf-8")
    return path


def _texts(uri: str) -> _Version:
    return _Version(f"text for {uri}", int(uri.rsplit("/", 1)[-1]))


def test_version_comment_is_a_pin_and_name_stays_upstream(tmp_path: Path) -> None:
    from databricks_agentkit.runtime.prompt_registry import schema_of

    from apx_agent._agent_toml import load_agent_toml

    path = _write(
        tmp_path,
        'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n'
        'judge = "main.prompts.reviewer"\n'
        'alias = "main.prompts.alias" # prompts:/main.prompts.alias@production\n',
    )
    verify(Artifact(str(path), min_bytes=80, must_contain="prompts:/main.prompts.writer/7"))
    project = load_agent_toml(tmp_path)
    assert project is not None
    assert project.prompts["writer"] == "main.prompts.writer"
    assert schema_of(project.prompts["writer"]) == "main.prompts"
    bindings = {binding.key: binding for binding in read_prompt_bindings(tmp_path)}
    assert bindings["writer"].uri == "prompts:/main.prompts.writer/7"
    assert bindings["writer"].version == 7
    assert bindings["judge"].uri is None
    assert bindings["judge"].alias is False
    assert bindings["alias"].uri is None
    assert bindings["alias"].alias is True


def test_uri_value_round_trips_upstream_and_apx_rejects_it(tmp_path: Path) -> None:
    from databricks_agentkit.runtime.prompt_registry import schema_of

    from apx_agent._agent_toml import load_agent_toml

    _write(tmp_path, 'writer = "prompts:/main.prompts.writer/7"\n')
    project = load_agent_toml(tmp_path)
    assert project is not None
    assert schema_of(project.prompts["writer"]) == "prompts:/main.prompts"
    binding = read_prompt_bindings(tmp_path)[0]
    assert binding.error is not None
    assert "schema_of" in binding.error
    assert 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7' in binding.error
    with pytest.raises(PromptPinError, match="schema_of"):
        apply_pinned_prompts(LlmAgent(instructions="local"), tmp_path, loader=_texts)


def test_startup_loads_one_plain_pin_as_instructions(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')
    agent = LlmAgent(instructions="local")
    loaded = apply_pinned_prompts(agent, tmp_path, loader=_texts)
    assert loaded[0].template == "text for prompts:/main.prompts.writer/7"
    assert agent._instructions == loaded[0].template
    calls = {"n": 0}

    def counting(uri: str) -> _Version:
        calls["n"] += 1
        return _texts(uri)

    again = apply_pinned_prompts(agent, tmp_path, loader=counting)
    assert again[0].uri == loaded[0].uri
    assert calls["n"] == 0


def test_template_and_extra_plain_pins_stay_named(tmp_path: Path) -> None:
    _write(
        tmp_path,
        'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n'
        'note = "main.prompts.note" # prompts:/main.prompts.note/2\n',
    )
    agent = LlmAgent(instructions="local")
    loaded = apply_pinned_prompts(
        agent,
        tmp_path,
        loader=lambda uri: _Version("Hello {{ name }}" if uri.endswith("/2") else "plain", int(uri.rsplit("/", 1)[-1])),
    )
    assert agent._instructions == "local"
    by_key = {prompt.key: prompt for prompt in loaded}
    assert by_key["note"].variables == ("name",)
    assert by_key["writer"].variables == ()
    assert by_key["writer"].template == "plain"


def test_instructions_key_wins_when_several_pins_are_plain(tmp_path: Path) -> None:
    _write(
        tmp_path,
        'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n'
        'instructions = "main.prompts.system" # prompts:/main.prompts.system/3\n',
    )
    agent = LlmAgent(instructions="local")
    apply_pinned_prompts(agent, tmp_path, loader=_texts)
    assert agent._instructions == "text for prompts:/main.prompts.system/3"


def test_missing_prompt_fails_startup_with_the_uri(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')

    def deny(uri: str) -> _Version:
        raise PermissionError(f"PERMISSION_DENIED for {uri}")

    with pytest.raises(PromptLoadError, match="PERMISSION_DENIED") as raised:
        apply_pinned_prompts(LlmAgent(instructions="local"), tmp_path, loader=deny)
    assert "prompts:/main.prompts.writer/7" in str(raised.value)
    assert "main.prompts" in str(raised.value)


def test_loaded_version_must_match_the_pin(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')
    with pytest.raises(PromptLoadError, match="loaded version 8"):
        apply_pinned_prompts(LlmAgent(instructions="local"), tmp_path, loader=lambda uri: _Version("x", 8))


def test_compile_agent_loads_the_pin_before_the_server_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from apx_agent._runtime_targets import compile_agent

    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')
    monkeypatch.setattr("apx_agent._prompt_pins._default_loader", _texts)
    agent = LlmAgent(instructions="local")
    app = compile_agent(
        agent,
        target="durable_agent_server",
        model="test-model",
        project_root=tmp_path,
        service_ws=MagicMock(),
    )
    assert type(app).__name__ == "DurableAgentServer"
    assert agent._instructions == "text for prompts:/main.prompts.writer/7"


def test_name_only_binding_is_not_loaded(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer"\n')
    agent = LlmAgent(instructions="local")

    def deny(uri: str) -> _Version:
        raise AssertionError(uri)

    assert apply_pinned_prompts(agent, tmp_path, loader=deny) == []
    assert agent._instructions == "local"


def test_doctor_offline_does_not_contact_the_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent._doctor import run_checks

    _write(
        tmp_path,
        'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n'
        'judge = "main.prompts.reviewer"\n',
    )
    def deny(uri: str) -> _Version:
        raise AssertionError(uri)

    monkeypatch.setattr("apx_agent._prompt_pins._default_loader", deny)
    rows = check_prompt_pins(tmp_path, online=False, auth_ok=True)
    by_name = {row.name: row for row in rows}
    assert by_name["Prompt registry (writer)"].status is Status.SKIP
    assert "registry not contacted" in by_name["Prompt registry (writer)"].detail
    assert "prompts:/main.prompts.writer/7" in by_name["Prompt registry (writer)"].detail
    assert by_name["Prompt registry (judge)"].status is Status.WARN
    assert "does not load" in by_name["Prompt registry (judge)"].detail
    checks = [check for _, group in run_checks(tmp_path, online=False) for check in group]
    assert any(check.name == "Prompt registry (writer)" and check.status is Status.SKIP for check in checks)


def test_doctor_skips_the_registry_without_auth(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')

    def deny(uri: str) -> _Version:
        raise AssertionError(uri)

    rows = check_prompt_pins(tmp_path, online=True, auth_ok=False, loader=deny)
    assert rows[0].status is Status.SKIP
    assert "auth is not configured" in rows[0].detail


def test_doctor_online_reports_load_success_and_denial(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')
    ok = check_prompt_pins(tmp_path, online=True, auth_ok=True, loader=_texts)
    assert ok[0].status is Status.OK
    assert "loaded prompts:/main.prompts.writer/7" in ok[0].detail

    def deny(uri: str) -> _Version:
        raise PermissionError("PERMISSION_DENIED")

    failed = check_prompt_pins(tmp_path, online=True, auth_ok=True, loader=deny)
    assert failed[0].status is Status.FAIL
    assert "PERMISSION_DENIED" in failed[0].detail
    assert "prompts:/main.prompts.writer/7" in failed[0].detail
    assert failed[0].fix is not None
    assert "main.prompts" in failed[0].fix


def test_trace_tags_follow_the_loaded_pin(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from apx_agent._resolved_versions import stamp_resolved_versions

    _write(
        tmp_path,
        'b = "main.prompts.b" # prompts:/main.prompts.b/2\n'
        'a = "main.prompts.a" # prompts:/main.prompts.a/1\n',
    )
    apply_pinned_prompts(LlmAgent(instructions="local"), tmp_path, loader=_texts)
    span = MagicMock()  # a nested span: parent_id is set, so no upstream link
    with patch("apx_agent._mlflow_tracing.set_trace_tags") as tags:
        stamp_resolved_versions(span)
    tags.assert_called_once_with({
        "apx.prompt.key": "a,b",
        "apx.prompt.uri": "prompts:/main.prompts.a/1,prompts:/main.prompts.b/2",
        "apx.prompt.version": "1,2",
    })
    span.set_attribute.assert_any_call("apx.prompt.uri", "prompts:/main.prompts.a/1,prompts:/main.prompts.b/2")


def test_agent_span_stamps_prompt_tags(tmp_path: Path) -> None:
    pytest.importorskip("mlflow")
    from unittest.mock import MagicMock, patch

    from apx_agent._mlflow_tracing import safe_span

    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')
    apply_pinned_prompts(LlmAgent(instructions="local"), tmp_path, loader=_texts)
    mock_span = MagicMock()
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=mock_span)
    context.__exit__ = MagicMock(return_value=None)
    with patch("mlflow.start_span", return_value=context), patch("apx_agent._mlflow_tracing.set_trace_tags") as tags:
        with safe_span("turn", span_type="AGENT"):
            pass
    assert any(call.args[0].get("apx.prompt.version") == "7" for call in tags.call_args_list)


def test_evaluate_tags_the_run_with_the_loaded_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mlflow")
    from unittest.mock import MagicMock, patch

    from apx_agent._eval import evaluate

    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("apx_agent._prompt_pins._default_loader", _texts)
    agent = LlmAgent(instructions="local")
    fake_eval = MagicMock(return_value="ok")
    with patch("apx_agent._eval.compile_to_chat_agent", return_value=MagicMock()), \
         patch("mlflow.genai.evaluate", fake_eval), \
         patch("mlflow.active_run", return_value=None), \
         patch("mlflow.start_run") as started, \
         patch("mlflow.set_tags") as set_tags, \
         patch("mlflow.end_run") as ended:
        assert evaluate(agent, model="m", evalset=[{"request": "hi"}], scorers=[]) == "ok"
    assert agent._instructions == "text for prompts:/main.prompts.writer/7"
    started.assert_called_once()
    set_tags.assert_called_once_with({
        "apx.prompt.key": "writer",
        "apx.prompt.uri": "prompts:/main.prompts.writer/7",
        "apx.prompt.version": "7",
    })
    ended.assert_called_once()
    fake_eval.assert_called_once()


def test_bump_rewrites_only_the_version_and_upstream_still_loads_the_name(tmp_path: Path) -> None:
    from databricks_agentkit.runtime.prompt_registry import schema_of

    from apx_agent._agent_toml import load_agent_toml

    _write(tmp_path, "")
    result = subprocess.run(
        [sys.executable, "-m", "databricks_agentbricks.cli.app", "experimental", "prompts", "bind",
         "main.prompts.writer", "--source", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    path = tmp_path / "agent.toml"
    text = path.read_text(encoding="utf-8")
    needle = 'writer = "main.prompts.writer"'
    assert needle in text
    path.write_text(text.replace(needle, needle + " # prompts:/main.prompts.writer/7", 1), encoding="utf-8")
    updated = bump_prompt_pin(tmp_path, "writer", 8)
    assert updated.uri == "prompts:/main.prompts.writer/8"
    verify(Artifact(str(path), min_bytes=80, must_contain="prompts:/main.prompts.writer/8"))
    project = load_agent_toml(tmp_path)
    assert project is not None
    assert project.prompts["writer"] == "main.prompts.writer"
    assert schema_of(project.prompts["writer"]) == "main.prompts"


def test_bump_refuses_an_alias_or_a_name_only_binding(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer@production\n')
    with pytest.raises(PromptPinError, match="does not follow aliases"):
        bump_prompt_pin(tmp_path, "writer", 4)


def test_open_prompt_pin_pr_bumps_before_gh(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.writer/7\n')
    seen: list[list[str]] = []

    def runner(command: list[str], cwd: Path) -> str:
        assert cwd == tmp_path
        assert "prompts:/main.prompts.writer/8" in (cwd / "agent.toml").read_text(encoding="utf-8")
        seen.append(command)
        if command[0] == "gh":
            return "https://example.invalid/pull/8\n"
        return ""

    url = open_prompt_pin_pr(tmp_path, "writer", 8, runner=runner)
    assert url == "https://example.invalid/pull/8"
    assert seen[0][:2] == ["git", "-C"]
    assert seen[1][:3] == ["git", "-C", str(tmp_path)]
    assert "agent.toml" in seen[1]
    assert seen[2][3] == "commit"
    assert seen[3][0] == "gh"
    assert any("prompts:/main.prompts.writer/8" in part for part in seen[3])
    assert any("prompts:/main.prompts.writer/7" in part for part in seen[3])


def test_comment_name_must_match_the_binding(tmp_path: Path) -> None:
    _write(tmp_path, 'writer = "main.prompts.writer" # prompts:/main.prompts.other/7\n')
    binding = read_prompt_bindings(tmp_path)[0]
    assert binding.error is not None
    assert "main.prompts.other" in binding.error
