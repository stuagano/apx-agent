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


def test_register_writes_extra_version_tags(patched: dict[str, Any]) -> None:
    client = _FakeMlflowClient()
    register_apps_manifest(
        object(),
        uc_name="main.agents.my_app",
        model="m",
        app_name="my-app",
        bundle_target="canary-v42",
        mlflow_client=client,
        extra_version_tags={"apx.apps.role": "canary"},
    )
    tagged = {(key, value) for _n, _v, key, value in client.version_tags}
    assert ("apx.apps.role", "canary") in tagged
    # base manifest tags still written
    assert (SERVING_TAG, "apps") in tagged


class _FakeModelVersion:
    def __init__(self, version: str, tags: dict[str, str]) -> None:
        self.version = version
        self.tags = tags


class _FakeQueryClient:
    """Fake MlflowClient for version-search + alias helpers."""

    def __init__(self, versions: list, alias_version: str | None = None) -> None:
        self._versions = versions
        self._alias_version = alias_version
        self.alias_set: tuple[str, str, str] | None = None

    def search_model_versions(self, filter_string: str) -> list:
        return list(self._versions)

    def get_model_version_by_alias(self, name: str, alias: str):
        if self._alias_version is None:
            raise RuntimeError("alias not found")
        return _FakeModelVersion(self._alias_version, {})

    def set_registered_model_alias(self, name: str, alias: str, version: str) -> None:
        self.alias_set = (name, alias, version)


def test_find_latest_canary_version_picks_highest_with_sha() -> None:
    from apx_agent._apps_registry import find_latest_canary_version

    client = _FakeQueryClient(versions=[
        _FakeModelVersion("3", {"apx.apps.role": "canary", "apx.apps.git_sha": "old"}),
        _FakeModelVersion("5", {"apx.apps.role": "canary", "apx.apps.git_sha": "new"}),
        _FakeModelVersion("4", {"apx.serving": "apps"}),  # prod manifest, not canary
    ])
    cm = find_latest_canary_version("main.agents.my_app", mlflow_client=client)
    assert cm is not None
    assert cm.version == "5"
    assert cm.git_sha == "new"


def test_find_latest_canary_version_none_when_no_canary() -> None:
    from apx_agent._apps_registry import find_latest_canary_version

    client = _FakeQueryClient(versions=[
        _FakeModelVersion("4", {"apx.serving": "apps"}),
    ])
    assert find_latest_canary_version("main.agents.my_app", mlflow_client=client) is None


def test_find_latest_canary_version_sha_none_when_tag_absent() -> None:
    from apx_agent._apps_registry import find_latest_canary_version

    client = _FakeQueryClient(versions=[
        _FakeModelVersion("2", {"apx.apps.role": "canary"}),  # no git_sha tag
    ])
    cm = find_latest_canary_version("main.agents.my_app", mlflow_client=client)
    assert cm is not None and cm.version == "2" and cm.git_sha is None


def test_prod_alias_get_and_set() -> None:
    from apx_agent._apps_registry import get_prod_alias_version, set_prod_alias_version

    client = _FakeQueryClient(versions=[], alias_version="7")
    assert get_prod_alias_version("main.agents.my_app", mlflow_client=client) == "7"

    none_client = _FakeQueryClient(versions=[], alias_version=None)
    assert get_prod_alias_version("main.agents.my_app", mlflow_client=none_client) is None

    set_prod_alias_version("main.agents.my_app", "9", mlflow_client=client)
    assert client.alias_set == ("main.agents.my_app", "prod", "9")


def test_get_latest_apps_version() -> None:
    from apx_agent._apps_registry import get_latest_apps_version

    client = _FakeQueryClient(versions=[
        _FakeModelVersion("3", {}), _FakeModelVersion("10", {}), _FakeModelVersion("7", {}),
    ])
    assert get_latest_apps_version("main.agents.my_app", mlflow_client=client) == "10"
    empty = _FakeQueryClient(versions=[])
    assert get_latest_apps_version("main.agents.my_app", mlflow_client=empty) is None
