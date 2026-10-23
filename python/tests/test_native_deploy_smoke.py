import io
import json
from types import SimpleNamespace

import click
import pytest
from pydantic import BaseModel, ConfigDict, RootModel

from apx_agent import AgentConfig, LlmAgent, cli
from click.testing import CliRunner


class Domain(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


def test_typed_smoke_requires_input_and_preserves_domain(tmp_path):
    agent = LlmAgent(name="typed", input_schema=Domain, input_key="domain")
    with pytest.raises(click.ClickException, match="--smoke-input"):
        cli._prepare_native_smoke_input(agent, None, readyz_gate=True)
    path = tmp_path / "smoke.json"
    path.write_text('{"value": 1}')
    payload = cli._prepare_native_smoke_input(agent, path, readyz_gate=True)
    assert payload["messages"] == []
    assert payload["apx_data_parts"][0]["data"] == {"value": 1}


@pytest.mark.parametrize("value", ['{"value": "private-value"}', '[]', '{bad', ' ' * 65537])
def test_invalid_smoke_input_is_bounded_and_redacted(tmp_path, value):
    path = tmp_path / "smoke.json"
    path.write_text(value)
    agent = LlmAgent(name="typed", input_schema=Domain, input_key="domain")
    with pytest.raises(click.ClickException) as exc:
        cli._prepare_native_smoke_input(agent, path, readyz_gate=True)
    assert "private-value" not in str(exc.value)


@pytest.mark.parametrize("output", [None, {"value": "invalid"}, {"value": 2}])
@pytest.mark.parametrize("content", ["answer", [{"type": "text", "text": "answer"}]])
def test_typed_smoke_checks_output(monkeypatch, output, content):
    monkeypatch.setattr(cli, "_run_databricks_cmd", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"access_token":"test"}'))
    def respond(req, timeout):
        sent = json.loads(req.data)
        assert sent["input"] == {"messages": []}
        extra = {} if output is None else {"apx_data_parts": [{"kind": "data", "data": output}]}
        return io.BytesIO(json.dumps({"output": {"status": "completed", "messages": [{"type": "ai", "content": content, "additional_kwargs": extra}]}}).encode())
    monkeypatch.setattr("urllib.request.urlopen", respond)
    result = cli._check_native_execution("https://example", profile=None, session_store=None,
                                         smoke_input={"messages": []}, smoke_agent=LlmAgent(name="typed", output_schema=Domain))
    assert result.completed == (output == {"value": 2})


def test_typed_smoke_fails_before_deploy_provisioning(tmp_path, monkeypatch):
    agent = LlmAgent(name="typed", input_schema=Domain, input_key="domain")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "typed"\nversion = "0.1.0"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "compare_pinned_sha", lambda cwd: SimpleNamespace(matches=True, skipped=True))
    config = AgentConfig(name="typed", target="durable_agent_server", deploy={"backend": "apps_api", "entrypoint": "app.py"})
    monkeypatch.setattr(cli, "_config_from_project_or_agent", lambda *a: (config, agent))
    monkeypatch.setattr(cli, "_preflight_databricks_cli", lambda: None)
    monkeypatch.setattr(cli, "_preflight_apps", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_make_scaffold_workspace_client", lambda *a: SimpleNamespace())
    def prerequisites(*args, **kwargs):
        assert not kwargs.get("provision_stores")
        return []
    monkeypatch.setattr(cli._doctor_mod, "check_agent_prerequisites", prerequisites)
    result = CliRunner().invoke(cli.main, ["agents", "deploy", "--target", "apps"])
    assert result.exit_code != 0
    assert "--smoke-input" in result.output
    assert not (tmp_path / ".build").exists()


@pytest.mark.parametrize("args", [["--target", "model-serving"], ["example.yaml"]])
def test_smoke_input_rejected_for_unsupported_destinations(tmp_path, args):
    path = tmp_path / "smoke.json"
    path.write_text('{"value":1}')
    result = CliRunner().invoke(cli.main, ["agents", "deploy", *args, "--smoke-input", str(path)])
    assert result.exit_code != 0
    assert "--smoke-input" in result.output


@pytest.mark.parametrize("checkpoint", [None, {}, {"thread_id": "bad"}, {"thread_id": "a" * 64, "principal": "forged"}])
def test_checkpoint_coordinate_required_for_managed_smoke(monkeypatch, checkpoint):
    monkeypatch.setattr(cli, "_run_databricks_cmd", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"access_token":"test"}'))
    body = {"output": {"status": "completed", "messages": [{"type": "ai", "content": "pong"}], "checkpoint": checkpoint}}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(json.dumps(body).encode()))
    result = cli._check_native_execution("https://example", profile=None, session_store="managed")
    assert not result.completed
    assert "checkpoint coordinate" in result.detail


@pytest.mark.parametrize("text,valid", [("[1,2]", True), ('["invalid"]', False)])
def test_smoke_validates_canonical_nonobject_output(monkeypatch, text, valid):
    monkeypatch.setattr(cli, "_run_databricks_cmd", lambda *a, **k: SimpleNamespace(returncode=0, stdout='{"access_token":"test"}'))
    body = {"output": {"status": "completed", "messages": [{"type": "ai", "content": [{"type": "text", "text": text}]}]}}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(json.dumps(body).encode()))
    result = cli._check_native_execution("https://example", profile=None, session_store=None,
                                         smoke_agent=LlmAgent(output_schema=RootModel[list[int]]))
    assert result.completed is valid
