"""Unit tests for ``apx_agent._apps_registry.register_apps_manifest``.

The registrar reuses the two serving-independent halves of the model-serving
flow — ``log_agent`` and ``set_uc_tags_for_agent`` — plus version-level tag
writes, and must NEVER touch ``databricks.agents`` (the App, not the artifact,
serves traffic). These tests mock MLflow at the seams so no run is created and
no network is hit.
"""

from __future__ import annotations

import contextlib
import sys
import types
from typing import Any

import pytest

from apx_agent._apps_registry import (
    APP_NAME_TAG,
    BUNDLE_TARGET_TAG,
    SERVING_TAG,
    AppsManifestResult,
    register_apps_manifest,
)


class _FakeModelInfo:
    def __init__(self, version: str) -> None:
        self.registered_model_version = version


class _FakeMlflowClient:
    """Captures ``set_model_version_tag`` calls."""

    def __init__(self) -> None:
        self.version_tags: list[tuple[str, str, str, str]] = []

    def set_model_version_tag(self, name: str, version: str, key: str, value: str) -> None:
        self.version_tags.append((name, version, key, value))


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch mlflow.start_run + log_agent + set_uc_tags_for_agent at their seams.

    Returns a dict capturing what each stub saw, so tests can assert on it.
    """
    captured: dict[str, Any] = {"log_agent": None, "set_uc_tags": None}

    # mlflow.start_run() must be a context manager; create a stub mlflow module
    # path is real (it's installed), so just patch the attribute.
    import mlflow

    monkeypatch.setattr(mlflow, "start_run", lambda *a, **k: contextlib.nullcontext())

    def _fake_log_agent(agent: Any, *, model: str, registered_model_name: str, **kw: Any) -> Any:
        captured["log_agent"] = {
            "agent": agent, "model": model, "uc_name": registered_model_name,
        }
        return _FakeModelInfo("7")

    def _fake_set_uc_tags(agent: Any, *, registered_model_name: str, model: str | None, name: str | None) -> dict:
        captured["set_uc_tags"] = {
            "uc_name": registered_model_name, "model": model, "name": name,
        }
        return {}

    monkeypatch.setattr("apx_agent._chat_agent.log_agent", _fake_log_agent)
    monkeypatch.setattr("apx_agent._watchdog.set_uc_tags_for_agent", _fake_set_uc_tags)
    return captured


def test_register_returns_version_from_log_agent(patched: dict[str, Any]) -> None:
    client = _FakeMlflowClient()
    res = register_apps_manifest(
        object(),
        uc_name="main.agents.my_app",
        model="databricks-claude-sonnet-4-6",
        app_name="my-app",
        bundle_target="dev",
        agent_name="My App",
        mlflow_client=client,
    )
    assert isinstance(res, AppsManifestResult)
    assert res.uc_name == "main.agents.my_app"
    assert res.version == "7"
    assert res.app_name == "my-app"


def test_register_writes_all_manifest_version_tags(patched: dict[str, Any]) -> None:
    client = _FakeMlflowClient()
    register_apps_manifest(
        object(),
        uc_name="main.agents.my_app",
        model="m",
        app_name="my-app",
        bundle_target="staging",
        mlflow_client=client,
    )
    tagged = {(key, value) for _name, _ver, key, value in client.version_tags}
    assert (SERVING_TAG, "apps") in tagged
    assert (APP_NAME_TAG, "my-app") in tagged
    assert (BUNDLE_TARGET_TAG, "staging") in tagged
    # All tags written against the resolved uc_name + version.
    assert all(name == "main.agents.my_app" and ver == "7"
               for name, ver, _k, _v in client.version_tags)


def test_register_calls_set_uc_tags_with_resolved_name(patched: dict[str, Any]) -> None:
    register_apps_manifest(
        object(),
        uc_name="main.agents.my_app",
        model="m",
        app_name="my-app",
        bundle_target="dev",
        agent_name="Friendly",
        mlflow_client=_FakeMlflowClient(),
    )
    assert patched["set_uc_tags"]["uc_name"] == "main.agents.my_app"
    assert patched["set_uc_tags"]["name"] == "Friendly"


def test_register_never_imports_databricks_agents(
    patched: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Apps manifest must never promote to serving — guard the import.

    Install a poisoned ``databricks.agents`` that explodes on attribute access,
    so any attempt to use it from the registrar fails loudly.
    """
    poisoned = types.ModuleType("databricks.agents")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("register_apps_manifest must not touch databricks.agents")

    poisoned.deploy = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks.agents", poisoned)

    register_apps_manifest(
        object(),
        uc_name="main.agents.my_app",
        model="m",
        app_name="my-app",
        bundle_target="dev",
        mlflow_client=_FakeMlflowClient(),
    )  # no AssertionError → never called deploy
