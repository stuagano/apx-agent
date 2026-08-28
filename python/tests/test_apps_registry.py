"""Unit tests for ``apx_agent._apps_registry.register_apps_manifest``.

The registrar writes a minimal non-serving MLflow artifact plus discovery tags
and must NEVER touch ``databricks.agents`` (the App, not the artifact, serves
traffic). These tests mock MLflow at the seams so no run is created and no
network is hit.
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
    _set_model_version_tag_with_retry,
    uc_safe_tag_key,
    register_apps_manifest,
)


class _FakeModelInfo:
    def __init__(self, version: str) -> None:
        self.registered_model_version = version


class _FakeMlflowClient:
    """Captures ``set_model_version_tag`` calls."""

    def __init__(self) -> None:
        self.version_tags: list[tuple[str, str, str, str]] = []
        self.registry_uris: list[str] = []

    def set_model_version_tag(self, name: str, version: str, key: str, value: str) -> None:
        import mlflow

        self.registry_uris.append(mlflow.get_registry_uri())
        self.version_tags.append((name, version, key, value))


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch mlflow.start_run + log_model + set_uc_tags_for_agent at their seams.

    Returns a dict capturing what each stub saw, so tests can assert on it.
    """
    captured: dict[str, Any] = {
        "log_model": None,
        "set_uc_tags": None,
        "start_run": None,
    }

    # mlflow.start_run() must be a context manager; create a stub mlflow module
    # path is real (it's installed), so just patch the attribute.
    import mlflow
    import mlflow.pyfunc

    def _fake_start_run(*args: Any, **kw: Any) -> Any:
        captured["start_run"] = {
            "args": args,
            "kwargs": kw,
            "tracking_uri": mlflow.get_tracking_uri(),
            "registry_uri": mlflow.get_registry_uri(),
        }
        return contextlib.nullcontext()

    monkeypatch.setattr(mlflow, "start_run", _fake_start_run)

    def _fake_log_model(**kw: Any) -> Any:
        captured["log_model"] = {
            **kw,
            "tracking_uri": mlflow.get_tracking_uri(),
            "registry_uri": mlflow.get_registry_uri(),
        }
        return _FakeModelInfo("7")

    def _fake_set_uc_tags(agent: Any, *, registered_model_name: str, model: str | None, name: str | None) -> dict:
        import mlflow

        captured["set_uc_tags"] = {
            "uc_name": registered_model_name, "model": model, "name": name,
            "registry_uri": mlflow.get_registry_uri(),
        }
        return {}

    monkeypatch.setattr(mlflow.pyfunc, "log_model", _fake_log_model)
    monkeypatch.setattr("apx_agent._watchdog.set_uc_tags_for_agent", _fake_set_uc_tags)
    return captured


def test_register_returns_version_from_manifest_log(patched: dict[str, Any]) -> None:
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
    assert patched["log_model"]["registered_model_name"] == "main.agents.my_app"
    assert patched["log_model"]["pip_requirements"] == []


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
    assert (uc_safe_tag_key(SERVING_TAG), "apps") in tagged
    assert (uc_safe_tag_key(APP_NAME_TAG), "my-app") in tagged
    assert (uc_safe_tag_key(BUNDLE_TARGET_TAG), "staging") in tagged
    # All tags written against the resolved uc_name + version.
    assert all(name == "main.agents.my_app" and ver == "7"
               for name, ver, _k, _v in client.version_tags)
    assert set(client.registry_uris) == {"databricks-uc"}


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
    assert patched["set_uc_tags"]["registry_uri"] == "databricks-uc"


def test_register_uses_explicit_profile_uris(patched: dict[str, Any]) -> None:
    register_apps_manifest(
        object(),
        uc_name="main.agents.my_app",
        model="m",
        app_name="my-app",
        bundle_target="dev",
        mlflow_client=_FakeMlflowClient(),
        profile="fevm",
    )
    assert patched["start_run"]["tracking_uri"] == "databricks://fevm"
    assert patched["start_run"]["registry_uri"] == "databricks-uc://fevm"
    assert patched["log_model"]["tracking_uri"] == "databricks://fevm"
    assert patched["log_model"]["registry_uri"] == "databricks-uc://fevm"
    assert patched["set_uc_tags"]["registry_uri"] == "databricks-uc://fevm"


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
    assert ("apx_apps_role", "canary") in tagged
    # base manifest tags still written
    assert (uc_safe_tag_key(SERVING_TAG), "apps") in tagged


def test_set_model_version_tag_retries_uc_visibility_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    calls = {"count": 0}

    class _FlakyClient:
        def set_model_version_tag(
            self, name: str, version: str, key: str, value: str,
        ) -> None:
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("Model Version (name=x, version=1) not found")

    monkeypatch.setattr("apx_agent._apps_registry.time.sleep", sleeps.append)

    _set_model_version_tag_with_retry(
        _FlakyClient(), "main.agents.my_app", "1", SERVING_TAG, "apps",
    )

    assert calls["count"] == 3
    assert sleeps == [2.0, 2.0]


def test_set_model_version_tag_does_not_retry_permission_errors() -> None:
    calls = {"count": 0}

    class _DeniedClient:
        def set_model_version_tag(
            self, name: str, version: str, key: str, value: str,
        ) -> None:
            calls["count"] += 1
            raise RuntimeError("PERMISSION_DENIED")

    with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
        _set_model_version_tag_with_retry(
            _DeniedClient(), "main.agents.my_app", "1", SERVING_TAG, "apps",
        )

    assert calls["count"] == 1


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

    def get_model_version(self, name: str, version: str):
        for candidate in self._versions:
            if str(candidate.version) == version:
                return candidate
        raise RuntimeError("version not found")

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


def test_get_latest_prod_version_filters_to_prod_role() -> None:
    """The @prod alias must never point at a canary version: only prod-tagged
    versions count, even when a canary version is numerically higher (which is
    exactly what get_latest_apps_version's max-over-all-roles would have returned)."""
    from apx_agent._apps_registry import get_latest_prod_version

    client = _FakeQueryClient(versions=[
        _FakeModelVersion("4", {"apx.apps.role": "prod"}),
        _FakeModelVersion("9", {"apx.apps.role": "canary"}),   # higher, but canary
        _FakeModelVersion("6", {"apx.apps.role": "prod"}),
    ])
    # Highest PROD version is 6 — not the canary 9.
    assert get_latest_prod_version("main.agents.my_app", mlflow_client=client) == "6"

    # No prod manifest → None (so @prod is left untouched, never set to a canary).
    no_prod = _FakeQueryClient(versions=[
        _FakeModelVersion("9", {"apx.apps.role": "canary"}),
    ])
    assert get_latest_prod_version("main.agents.my_app", mlflow_client=no_prod) is None


def test_version_readers_accept_uc_safe_tag_keys() -> None:
    from apx_agent._apps_registry import (
        GIT_SHA_TAG,
        ROLE_TAG,
        find_latest_canary_version,
        get_latest_prod_version,
    )

    client = _FakeQueryClient(versions=[
        _FakeModelVersion("1", {
            uc_safe_tag_key(ROLE_TAG): "canary",
            uc_safe_tag_key(GIT_SHA_TAG): "abc123",
        }),
        _FakeModelVersion("2", {uc_safe_tag_key(ROLE_TAG): "prod"}),
    ])

    canary = find_latest_canary_version("main.agents.my_app", mlflow_client=client)
    assert canary is not None
    assert canary.version == "1"
    assert canary.git_sha == "abc123"
    assert get_latest_prod_version("main.agents.my_app", mlflow_client=client) == "2"
