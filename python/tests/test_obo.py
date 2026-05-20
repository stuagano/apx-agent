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
# Precedence: custom_inputs wins
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


def test_precedence_user_id_custom_inputs_wins() -> None:
    obo = extract_obo_headers(
        custom_inputs={"user_id": "body-user"},
        headers={"X-Forwarded-User": "header-user"},
    )
    assert obo["user_id"] == "body-user"


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
