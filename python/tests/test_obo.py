"""Tests for ``apx_agent._obo`` — unified OBO header extraction.

Covers:

  1. Model Serving convention: ``custom_inputs.user_token`` + ``workspace_host``
     → normalized dict carries both fields untouched.
  2. Apps convention: ``X-Forwarded-Access-Token`` / ``-User`` / ``-Email``
     headers → normalized dict carries the same fields.
  3. Precedence: when BOTH conventions appear in one request,
     ``custom_inputs`` wins (existing semantics from ``_invocations.py``).
  4. ``DATABRICKS_HOST`` env var fallback for ``workspace_host``.
  5. Missing inputs handled gracefully — no exceptions, empty dict.
  6. Case-insensitive header lookup.
"""

from __future__ import annotations

import pytest

from apx_agent import extract_obo_headers


# ---------------------------------------------------------------------------
# Model Serving convention — custom_inputs
# ---------------------------------------------------------------------------


def test_custom_inputs_user_token_passthrough() -> None:
    obo = extract_obo_headers(
        custom_inputs={
            "user_token": "tok-abc",
            "workspace_host": "https://ws.cloud.databricks.com",
        },
    )
    assert obo["user_token"] == "tok-abc"
    assert obo["workspace_host"] == "https://ws.cloud.databricks.com"


def test_custom_inputs_with_user_id_and_email() -> None:
    obo = extract_obo_headers(
        custom_inputs={
            "user_token": "tok-abc",
            "user_id": "12345",
            "user_email": "alice@example.com",
        },
    )
    assert obo["user_token"] == "tok-abc"
    assert obo["user_id"] == "12345"
    assert obo["user_email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Apps convention — X-Forwarded-* headers
# ---------------------------------------------------------------------------


def test_apps_headers_extracted() -> None:
    headers = {
        "X-Forwarded-Access-Token": "tok-from-apps",
        "X-Forwarded-User": "user-99",
        "X-Forwarded-Email": "bob@example.com",
    }
    obo = extract_obo_headers(headers=headers)
    assert obo["user_token"] == "tok-from-apps"
    assert obo["user_id"] == "user-99"
    assert obo["user_email"] == "bob@example.com"


def test_apps_headers_case_insensitive() -> None:
    headers = {
        "x-forwarded-access-token": "tok-lower",
        "x-forwarded-user": "user-77",
    }
    obo = extract_obo_headers(headers=headers)
    assert obo["user_token"] == "tok-lower"
    assert obo["user_id"] == "user-77"


# ---------------------------------------------------------------------------
# Precedence: tokens body-wins; Apps identity proxy-wins (#615)
# ---------------------------------------------------------------------------


def test_precedence_custom_inputs_wins_over_headers() -> None:
    """custom_inputs.user_token MUST override an Apps-injected header.

    Mirrors the existing ``_invocations.py`` rule: a caller-supplied token in
    the request body wins over the proxy-injected one. This preserves the
    batch-eval pathway where callers explicitly thread a different token.
    """
    obo = extract_obo_headers(
        custom_inputs={"user_token": "tok-from-body"},
        headers={"X-Forwarded-Access-Token": "tok-from-header"},
    )
    assert obo["user_token"] == "tok-from-body"


def test_precedence_workspace_host_custom_inputs_wins() -> None:
    obo = extract_obo_headers(
        custom_inputs={"workspace_host": "https://body-ws.databricks.com"},
        headers={"X-Forwarded-Host": "https://header-ws.databricks.com"},
    )
    assert obo["workspace_host"] == "https://body-ws.databricks.com"


def test_precedence_user_id_custom_inputs_wins_outside_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside Apps, body user_id still wins (local / Model Serving)."""
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    obo = extract_obo_headers(
        custom_inputs={"user_id": "body-user"},
        headers={"X-Forwarded-User": "header-user"},
    )
    assert obo["user_id"] == "body-user"


def test_precedence_apps_proxy_user_id_beats_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#615: in Apps, X-Forwarded-User wins — body must not spoof principal."""
    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
    obo = extract_obo_headers(
        custom_inputs={"user_id": "alice-spoof", "user_email": "alice@evil"},
        headers={
            "X-Forwarded-User": "bob",
            "X-Forwarded-Email": "bob@company.com",
        },
    )
    assert obo["user_id"] == "bob"
    assert obo["user_email"] == "bob@company.com"


def test_apply_obo_overwrites_spoofed_identity() -> None:
    from apx_agent._obo import apply_obo_to_custom_inputs

    ci = {"user_id": "alice-spoof", "user_token": "body-tok"}
    apply_obo_to_custom_inputs(
        ci,
        {"user_id": "bob", "user_token": "hdr-tok"},
    )
    assert ci["user_id"] == "bob"
    assert ci["user_token"] == "body-tok"  # setdefault — body token wins


# ---------------------------------------------------------------------------
# Env var fallback for workspace_host
# ---------------------------------------------------------------------------


def test_workspace_host_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://env-ws.cloud.databricks.com")
    obo = extract_obo_headers(custom_inputs={"user_token": "tok"})
    assert obo["workspace_host"] == "https://env-ws.cloud.databricks.com"


def test_workspace_host_explicit_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://env-ws.cloud.databricks.com")
    obo = extract_obo_headers(
        custom_inputs={"workspace_host": "https://explicit.databricks.com"},
    )
    assert obo["workspace_host"] == "https://explicit.databricks.com"


def test_workspace_host_env_wins_over_forwarded_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DATABRICKS_HOST`` env takes precedence over ``X-Forwarded-Host``.

    In Databricks Apps the proxy injects ``X-Forwarded-Host`` as the public
    APP HOSTNAME (e.g. ``my-app-1234.aws.databricksapps.com``) — NOT the
    workspace API host. Using that header as ``workspace_host`` produces a
    WorkspaceClient that cannot reach the workspace REST API.

    The Apps runtime DOES inject ``DATABRICKS_HOST`` env with the correct
    workspace API host, so the env must win. Verified on fe-stable
    2026-05-20.
    """
    monkeypatch.setenv("DATABRICKS_HOST", "example-workspace.cloud.databricks.com")
    obo = extract_obo_headers(
        headers={
            "X-Forwarded-Access-Token": "tok",
            "X-Forwarded-Host": "my-app-1234.aws.databricksapps.com",
        },
    )
    assert obo["workspace_host"] == "example-workspace.cloud.databricks.com"


def test_workspace_host_falls_back_to_forwarded_host_when_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-resort fallback: header is used when no env, no custom_inputs —
    and NOT running inside a Databricks App (where the header is the app's own
    host). This protects non-Apps proxies that pass the workspace API host
    through X-Forwarded-Host."""
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    obo = extract_obo_headers(
        headers={"X-Forwarded-Host": "ws.databricks.com"},
    )
    assert obo["workspace_host"] == "ws.databricks.com"


def test_workspace_host_skips_forwarded_host_inside_databricks_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a Databricks App, ``X-Forwarded-Host`` is the app's OWN public
    hostname (…databricksapps.com), not the workspace REST API host. Using it
    as ``workspace_host`` makes user-scoped SDK calls loop back to the app
    (which doesn't serve /api/2.0/*) → 60s read-timeout × retries → a 5-minute
    hang. So when the App runtime markers are present and DATABRICKS_HOST is
    (anomalously) absent, do NOT fall back to the header — leave workspace_host
    unset so the SDK fails fast with a clear config error instead of hanging.
    Regression for the OBO host loopback hang.
    """
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.setenv("DATABRICKS_APP_NAME", "cowork-validation")
    obo = extract_obo_headers(
        headers={
            "X-Forwarded-Access-Token": "tok",
            "X-Forwarded-Host": "cowork-validation-123.aws.databricksapps.com",
        },
    )
    assert "workspace_host" not in obo
    # token still extracted — only the host fallback is suppressed.
    assert obo["user_token"] == "tok"


def test_workspace_host_env_still_wins_inside_databricks_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard only suppresses the X-Forwarded-Host *fallback*; the normal
    Apps path (DATABRICKS_HOST injected) is unaffected."""
    monkeypatch.setenv("DATABRICKS_APP_NAME", "cowork-validation")
    monkeypatch.setenv("DATABRICKS_HOST", "fevm-x.cloud.databricks.com")
    obo = extract_obo_headers(
        headers={
            "X-Forwarded-Access-Token": "tok",
            "X-Forwarded-Host": "cowork-validation-123.aws.databricksapps.com",
        },
    )
    assert obo["workspace_host"] == "fevm-x.cloud.databricks.com"


# ---------------------------------------------------------------------------
# Missing inputs / empty cases
# ---------------------------------------------------------------------------


def test_no_inputs_returns_empty_or_env_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    obo = extract_obo_headers()
    # No token, no host, no user info anywhere
    assert obo == {}


def test_none_custom_inputs_handled() -> None:
    obo = extract_obo_headers(custom_inputs=None, headers=None)
    # No throw; either empty or env-only
    assert isinstance(obo, dict)


def test_partial_apps_headers_only_token() -> None:
    obo = extract_obo_headers(
        headers={"X-Forwarded-Access-Token": "only-token"}
    )
    assert obo.get("user_token") == "only-token"
    assert "user_id" not in obo
    assert "user_email" not in obo


def test_starlette_like_headers_object_supported() -> None:
    """A getter-only object (Starlette ``Headers``) should still work."""

    class FakeStarletteHeaders:
        def __init__(self, data: dict[str, str]) -> None:
            self._data = {k.lower(): v for k, v in data.items()}

        def get(self, name: str, default=None):  # noqa: ANN001
            return self._data.get(name.lower(), default)

        def items(self):
            return list(self._data.items())

    fake = FakeStarletteHeaders(
        {"X-Forwarded-Access-Token": "tok-star", "X-Forwarded-User": "u-1"}
    )
    obo = extract_obo_headers(headers=fake)
    assert obo["user_token"] == "tok-star"
    assert obo["user_id"] == "u-1"


# ── G2: warn-once on app-SP fallback in the Apps runtime ──────────────────────


def test_warn_once_no_obo_fires_once_in_app(monkeypatch, caplog):
    """In the Apps runtime, a tokenless request warns exactly once per process."""
    import logging

    import apx_agent._obo as _obo

    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
    monkeypatch.setattr(_obo, "_warned_no_obo_in_app", False)
    with caplog.at_level(logging.WARNING, logger="apx_agent._obo"):
        _obo.warn_once_no_obo_in_app()
        _obo.warn_once_no_obo_in_app()  # second call must be silent
    fired = [r for r in caplog.records if "app service principal" in r.getMessage()]
    assert len(fired) == 1
    assert "G2" in fired[0].getMessage()


def test_warn_once_no_obo_silent_outside_app(monkeypatch, caplog):
    """Outside the Apps runtime (local / Model Serving SP), it stays silent."""
    import logging

    import apx_agent._obo as _obo

    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    monkeypatch.setattr(_obo, "_warned_no_obo_in_app", False)
    with caplog.at_level(logging.WARNING, logger="apx_agent._obo"):
        _obo.warn_once_no_obo_in_app()
    assert caplog.records == []


# ── G2: fail-closed identity policy (resolve_no_obo_or_raise) ──────────────────


def test_no_obo_in_app_rejects_by_default(monkeypatch):
    """In the Apps runtime with no token and no opt-in, the request is rejected."""
    import apx_agent._obo as _obo

    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
    monkeypatch.delenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", raising=False)
    with pytest.raises(_obo.ApxIdentityError, match="fails closed"):
        _obo.resolve_no_obo_or_raise()


def test_no_obo_in_app_allowed_with_optin(monkeypatch, caplog):
    """With the SP-fallback opt-in, it does NOT raise — only warns once."""
    import logging

    import apx_agent._obo as _obo

    monkeypatch.setenv("DATABRICKS_APP_NAME", "my-app")
    monkeypatch.setenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", "true")
    monkeypatch.setattr(_obo, "_warned_no_obo_in_app", False)
    with caplog.at_level(logging.WARNING, logger="apx_agent._obo"):
        _obo.resolve_no_obo_or_raise()  # must not raise
    assert any("app service principal" in r.getMessage() for r in caplog.records)


def test_no_obo_outside_app_is_noop(monkeypatch):
    """Outside the Apps runtime (local / Model Serving SP-default), it's a no-op."""
    import apx_agent._obo as _obo

    monkeypatch.delenv("DATABRICKS_APP_NAME", raising=False)
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    _obo.resolve_no_obo_or_raise()  # no raise, no error


def test_sp_fallback_env_truthy_values(monkeypatch):
    import apx_agent._obo as _obo

    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", val)
        assert _obo._sp_fallback_allowed() is True
    for val in ("0", "false", "no", ""):
        monkeypatch.setenv("APX_ALLOW_SERVICE_PRINCIPAL_FALLBACK", val)
        assert _obo._sp_fallback_allowed() is False


def test_scope_session_key_namespaces_by_principal() -> None:
    """#491: same raw session_id + different principals → isolated keys; same
    principal → stable; no principal → unchanged (local dev)."""
    from apx_agent._obo import scope_session_key

    a = scope_session_key({"session_id": "S", "user_id": "alice"}, "alice")
    b = scope_session_key({"session_id": "S", "user_id": "bob"}, "bob")
    assert a["session_id"] != b["session_id"]  # collision avoided across users
    assert a["session_id"].endswith(":S")

    # Same principal re-derives the same key (resumes their own thread).
    assert scope_session_key({"session_id": "S"}, "alice")["session_id"] == a["session_id"]

    # No / empty principal → unchanged (nothing to isolate by).
    assert scope_session_key({"session_id": "S"}, None)["session_id"] == "S"
    assert scope_session_key({"session_id": "S"}, "")["session_id"] == "S"

    # thread_id is namespaced too; a dict with neither key is untouched.
    assert scope_session_key({"thread_id": "T"}, "alice")["thread_id"].endswith(":T")
    assert scope_session_key({"user_id": "x"}, "x") == {"user_id": "x"}
    assert scope_session_key(None, "alice") is None
