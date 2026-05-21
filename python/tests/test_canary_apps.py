"""Tests for the Apps-target canary flow.

Covers ``_canary_apps.py`` (YAML mutation + orchestration) and the
``apx canary {deploy,promote,rollback,analyze} --target apps`` CLI
dispatch. All subprocess calls route through
``apx_agent.cli._run_databricks_cmd`` — we monkey-patch that single
seam so no real ``databricks`` CLI process spawns.

The fixture layout mirrors ``test_deploy_apps.py``: scaffold a minimal
bundle under ``tmp_path``, chdir into it, invoke the CLI, assert on
captured args + the on-disk YAML.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from apx_agent import (
    AppsCanaryConfig,
    AppsCanaryReport,
    AppsVersionMetrics,
    add_canary_target_to_yml,
    analyze_canary_app,
    canary_app_name,
    canary_target_name,
    deploy_canary_app,
    promote_canary_app,
    remove_canary_target_from_yml,
    rollback_canary_app,
    sanitize_version,
)
from apx_agent.cli import main


# ---------------------------------------------------------------------------
# Scaffold fixture (mirrors test_deploy_apps.py)
# ---------------------------------------------------------------------------


_DATABRICKS_YML = """\
bundle:
  name: test-app

variables:
  llm_endpoint_name:
    description: Foundation model endpoint.
    default: databricks-claude-sonnet-4-6

resources:
  apps:
    my-app:
      name: my-app
      description: test
      source_code_path: ./.build

targets:
  prod:
    default: true
    mode: production
"""


def _write_scaffold(tmp_path: Path) -> Path:
    (tmp_path / "databricks.yml").write_text(_DATABRICKS_YML)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test-app"\n')
    server = tmp_path / "agent_server"
    server.mkdir()
    (server / "__init__.py").write_text("")
    (server / "agent.py").write_text("agent = object()\n")
    return tmp_path


@pytest.fixture
def scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write_scaffold(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("agent_server", None)
    sys.modules.pop("agent_server.agent", None)
    return tmp_path


# ---------------------------------------------------------------------------
# Subprocess mock
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_mock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deploy_rc: int = 0,
    run_rc: int = 0,
    delete_rc: int = 0,
    apps_get_url: str = "https://canary.example.com",
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(args: list[str], profile: str | None = None) -> _FakeProc:
        calls.append(list(args))
        if args[:2] == ["bundle", "deploy"]:
            return _FakeProc(deploy_rc, stdout="deploy ok\n", stderr="")
        if args[:2] == ["bundle", "run"]:
            return _FakeProc(run_rc, stdout="run ok\n", stderr="")
        if args[:2] == ["apps", "get"]:
            return _FakeProc(0, stdout=json.dumps({
                "url": apps_get_url,
                "app_status": {"state": "RUNNING"},
                "compute_status": {"state": "ACTIVE"},
            }), stderr="")
        if args[:2] == ["apps", "delete"]:
            return _FakeProc(delete_rc, stdout="", stderr="")
        return _FakeProc(0, stdout="", stderr="")

    monkeypatch.setattr("apx_agent.cli._run_databricks_cmd", fake)
    return calls


# ---------------------------------------------------------------------------
# Pure-helper tests — no CLI / subprocess
# ---------------------------------------------------------------------------


def test_sanitize_version_lowercases_and_strips_unsafe_chars() -> None:
    assert sanitize_version("FeatX") == "featx"
    assert sanitize_version("v0.4.2") == "v0-4-2"
    assert sanitize_version("feature/foo_bar") == "feature-foo-bar"
    assert sanitize_version("  7f3ab9c  ") == "7f3ab9c"


def test_sanitize_version_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty string"):
        sanitize_version("///___")


def test_target_and_app_naming_is_deterministic() -> None:
    assert canary_target_name("v0.4.2") == "canary-v0-4-2"
    assert canary_app_name("my-app", "v0.4.2") == "my-app-canary-v0-4-2"


def test_add_canary_target_to_yml_inserts_target_block() -> None:
    doc = yaml.safe_load(_DATABRICKS_YML)
    add_canary_target_to_yml(
        doc, bundle_key="my-app", base_app_name="my-app", version="feat-x",
    )
    assert "canary-feat-x" in doc["targets"]
    target_block = doc["targets"]["canary-feat-x"]
    assert target_block["resources"]["apps"]["my-app"]["name"] == "my-app-canary-feat-x"
    # Did not touch the base target's name field.
    base_name = doc["resources"]["apps"]["my-app"]["name"]
    assert base_name == "my-app"


def test_add_canary_target_to_yml_raises_when_app_missing() -> None:
    doc = {"resources": {"apps": {}}}
    with pytest.raises(KeyError, match="resources.apps.my-app"):
        add_canary_target_to_yml(
            doc, bundle_key="my-app", base_app_name="my-app", version="v",
        )


def test_remove_canary_target_from_yml_returns_false_if_absent() -> None:
    doc: dict[str, Any] = {"targets": {"prod": {}}}
    assert remove_canary_target_from_yml(doc, version="missing") is False
    assert "prod" in doc["targets"]


def test_remove_canary_target_from_yml_removes_when_present() -> None:
    doc: dict[str, Any] = {"targets": {"prod": {}, "canary-v1": {}}}
    assert remove_canary_target_from_yml(doc, version="v1") is True
    assert "canary-v1" not in doc["targets"]


# ---------------------------------------------------------------------------
# deploy_canary_app — orchestration
# ---------------------------------------------------------------------------


def test_deploy_canary_app_writes_yml_and_invokes_bundle(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_mock(monkeypatch)
    from apx_agent.cli import _run_databricks_cmd  # noqa: F401 — patched above

    cfg = deploy_canary_app(
        cwd=scaffold,
        bundle_key="my-app",
        base_app_name="my-app",
        canary_version="feat-x",
        traffic_hint=10,
        run_cmd=_run_databricks_cmd,
        profile="someprofile",
    )

    # YAML mutation landed on disk.
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    assert "canary-feat-x" in doc["targets"]
    assert doc["targets"]["canary-feat-x"]["resources"]["apps"]["my-app"]["name"] \
        == "my-app-canary-feat-x"

    # Subprocess calls dispatched in the right order.
    assert ["bundle", "deploy", "--target", "canary-feat-x"] in calls
    assert ["bundle", "run", "my-app", "--target", "canary-feat-x"] in calls
    assert ["apps", "get", "my-app-canary-feat-x"] in calls

    # Result shape.
    assert isinstance(cfg, AppsCanaryConfig)
    assert cfg.bundle_target == "canary-feat-x"
    assert cfg.canary_app_name == "my-app-canary-feat-x"
    assert cfg.traffic_hint == 10


def test_deploy_canary_app_raises_when_deploy_fails(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch, deploy_rc=1)
    from apx_agent.cli import _run_databricks_cmd

    with pytest.raises(RuntimeError, match="bundle deploy"):
        deploy_canary_app(
            cwd=scaffold,
            bundle_key="my-app",
            base_app_name="my-app",
            canary_version="v",
            traffic_hint=5,
            run_cmd=_run_databricks_cmd,
        )


def test_promote_canary_app_redeploys_prod_and_tears_down(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed the YAML with an existing canary target so teardown has something
    # to remove.
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    add_canary_target_to_yml(
        doc, bundle_key="my-app", base_app_name="my-app", version="feat-x",
    )
    (scaffold / "databricks.yml").write_text(yaml.safe_dump(doc))

    calls = _install_mock(monkeypatch)
    from apx_agent.cli import _run_databricks_cmd

    result = promote_canary_app(
        cwd=scaffold,
        bundle_key="my-app",
        base_app_name="my-app",
        canary_version="feat-x",
        run_cmd=_run_databricks_cmd,
    )

    # Prod redeploy.
    assert ["bundle", "deploy", "--target", "prod"] in calls
    assert ["bundle", "run", "my-app", "--target", "prod"] in calls
    # Teardown.
    assert ["apps", "delete", "my-app-canary-feat-x"] in calls
    # YAML target removed.
    new_doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    assert "canary-feat-x" not in (new_doc.get("targets") or {})
    assert result.canary_target_removed is True
    assert result.prod_app_name == "my-app"


def test_promote_canary_app_keep_canary_flag(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    add_canary_target_to_yml(
        doc, bundle_key="my-app", base_app_name="my-app", version="feat-x",
    )
    (scaffold / "databricks.yml").write_text(yaml.safe_dump(doc))

    calls = _install_mock(monkeypatch)
    from apx_agent.cli import _run_databricks_cmd

    result = promote_canary_app(
        cwd=scaffold,
        bundle_key="my-app",
        base_app_name="my-app",
        canary_version="feat-x",
        run_cmd=_run_databricks_cmd,
        keep_canary=True,
    )

    # No apps delete dispatched.
    assert not any(c[:2] == ["apps", "delete"] for c in calls)
    # YAML target still present.
    new_doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    assert "canary-feat-x" in (new_doc.get("targets") or {})
    assert result.canary_target_removed is False


def test_rollback_canary_app_invokes_promote_path(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rollback is functionally identical to promote — same CLI dispatch."""
    calls = _install_mock(monkeypatch)
    from apx_agent.cli import _run_databricks_cmd

    result = rollback_canary_app(
        cwd=scaffold,
        bundle_key="my-app",
        base_app_name="my-app",
        canary_version="prev-tag",
        run_cmd=_run_databricks_cmd,
    )
    assert ["bundle", "deploy", "--target", "prod"] in calls
    assert result.promoted_from_version == "prev-tag"


# ---------------------------------------------------------------------------
# analyze_canary_app — MLflow trace partitioning
# ---------------------------------------------------------------------------


def _trace_dict(app_name: str | None, latency_ms: int, status: str = "OK") -> dict:
    """Build a dict-shaped trace matching the format mlflow.search_traces
    returns under to_dict(orient='records')."""
    return {
        "tags": {"apx.app.name": app_name} if app_name else {},
        "execution_time_ms": latency_ms,
        "status": status,
    }


def test_analyze_canary_app_partitions_by_app_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mlflow_stub = SimpleNamespace()
    fake_rows = [
        _trace_dict("my-app", 100),
        _trace_dict("my-app", 200),
        _trace_dict("my-app", 300, status="ERROR"),
        _trace_dict("my-app-canary-feat-x", 50),
        _trace_dict("my-app-canary-feat-x", 60),
        _trace_dict("some-other-app", 999),  # filtered out
    ]
    mlflow_stub.search_traces = lambda **_: fake_rows  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)

    report = analyze_canary_app(
        prod_app_name="my-app",
        canary_app_name="my-app-canary-feat-x",
        experiment="my_experiment",
        lookback_hours=24,
    )

    assert isinstance(report, AppsCanaryReport)
    assert len(report.apps) == 2
    prod = report.by_name("my-app")
    canary = report.by_name("my-app-canary-feat-x")
    assert prod is not None and canary is not None
    assert prod.requests == 3
    assert prod.errors == 1
    assert canary.requests == 2
    assert canary.errors == 0
    # P95 of the small canary set lands at 60.
    assert canary.latency_p95_ms == 60
    # Best by latency = canary (lower p95).
    best = report.best_by_latency()
    assert best is not None and best.app_name == "my-app-canary-feat-x"
    # Best by error rate = canary (0% vs 33%).
    best_err = report.best_by_error_rate()
    assert best_err is not None and best_err.app_name == "my-app-canary-feat-x"


def test_analyze_canary_app_returns_zero_buckets_when_no_traces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mlflow_stub = SimpleNamespace()
    mlflow_stub.search_traces = lambda **_: []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)

    report = analyze_canary_app(
        prod_app_name="my-app",
        canary_app_name="my-app-canary-x",
        experiment="exp",
        lookback_hours=1,
    )
    # Still returns rows for both apps (with zeros) so the comparison
    # output reads as "0 vs 0" rather than collapsing.
    assert len(report.apps) == 2
    assert all(a.requests == 0 for a in report.apps)


def test_analyze_canary_app_handles_search_traces_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mlflow_stub = SimpleNamespace()

    def _raise(**_: Any) -> Any:
        raise RuntimeError("transient")

    mlflow_stub.search_traces = _raise  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)

    report = analyze_canary_app(
        prod_app_name="my-app", canary_app_name="my-app-canary-x",
        experiment="exp", lookback_hours=1,
    )
    # Empty report — no apps, doesn't raise.
    assert report.apps == ()


# ---------------------------------------------------------------------------
# CLI dispatch — `apx canary deploy --target apps`
# ---------------------------------------------------------------------------


def test_cli_canary_deploy_apps_writes_yml_and_runs_bundle(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["canary", "deploy", "--target", "apps", "--canary-version", "feat-x",
         "--traffic", "20"],
    )
    assert result.exit_code == 0, result.output
    # YAML mutated.
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    assert "canary-feat-x" in doc["targets"]
    # Subprocess dispatched.
    assert ["bundle", "deploy", "--target", "canary-feat-x"] in calls


def test_cli_canary_deploy_apps_requires_canary_version(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main, ["canary", "deploy", "--target", "apps"],
    )
    assert result.exit_code != 0
    assert "--canary-version" in result.output


def test_cli_canary_deploy_default_target_still_model_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the no --target flag path didn't regress. Patches the SDK
    side so this test doesn't need a real WorkspaceClient."""
    import apx_agent.cli as cli_mod

    captured: dict[str, Any] = {}

    def fake_deploy(**kwargs: Any) -> Any:
        captured.update(kwargs)
        # Return a minimal CanaryConfig stand-in.
        return SimpleNamespace(traffic_split={"a-1": 90, "a-2": 10})

    monkeypatch.setattr("apx_agent.deploy_canary", fake_deploy)
    monkeypatch.setattr(
        "databricks.sdk.WorkspaceClient",
        lambda *a, **k: SimpleNamespace(),
        raising=False,
    )
    # Provide a click context.
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["canary", "deploy",
         "--endpoint", "ep",
         "--model", "cat.sch.mod",
         "--version", "5",
         "--traffic", "10"],
    )
    assert result.exit_code == 0, result.output
    assert captured.get("endpoint") == "ep"
    assert captured.get("canary_traffic_pct") == 10


def test_cli_canary_promote_apps_dispatches_correctly(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed a canary block so the teardown step has something to remove.
    doc = yaml.safe_load((scaffold / "databricks.yml").read_text())
    add_canary_target_to_yml(
        doc, bundle_key="my-app", base_app_name="my-app", version="feat-x",
    )
    (scaffold / "databricks.yml").write_text(yaml.safe_dump(doc))

    calls = _install_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["canary", "promote", "--target", "apps", "--canary-version", "feat-x"],
    )
    assert result.exit_code == 0, result.output
    assert ["bundle", "deploy", "--target", "prod"] in calls
    assert ["apps", "delete", "my-app-canary-feat-x"] in calls


def test_cli_canary_rollback_apps_dispatches_correctly(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["canary", "rollback", "--target", "apps", "--canary-version", "prev"],
    )
    assert result.exit_code == 0, result.output
    assert ["bundle", "deploy", "--target", "prod"] in calls


def test_cli_canary_analyze_apps_uses_experiment_from_pyproject(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub mlflow before invoking.
    mlflow_stub = SimpleNamespace()
    mlflow_stub.search_traces = lambda **_: [  # type: ignore[attr-defined]
        _trace_dict("my-app", 100),
        _trace_dict("my-app-canary-x", 50),
    ]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)

    # pyproject.toml provides the experiment fallback.
    (scaffold / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "test-app"

        [tool.apx.agent]
        experiment = "/Workspace/Users/me/exp"
    """))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["canary", "analyze", "--target", "apps", "--canary-version", "x"],
    )
    assert result.exit_code == 0, result.output
    assert "my-app" in result.output
    assert "my-app-canary-x" in result.output


def test_cli_canary_analyze_apps_json_output(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mlflow_stub = SimpleNamespace()
    mlflow_stub.search_traces = lambda **_: [  # type: ignore[attr-defined]
        _trace_dict("my-app", 100),
    ]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)
    (scaffold / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "test-app"

        [tool.apx.agent]
        experiment = "/exp"
    """))

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["canary", "analyze", "--target", "apps",
         "--canary-version", "x", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["prod_app_name"] == "my-app"
    assert payload["canary_app_name"] == "my-app-canary-x"
    assert isinstance(payload["apps"], list)


def test_cli_canary_promote_apps_requires_canary_version(
    scaffold: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mock(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        main, ["canary", "promote", "--target", "apps"],
    )
    assert result.exit_code != 0
    assert "--canary-version" in result.output
