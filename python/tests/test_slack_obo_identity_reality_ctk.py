"""Claim-vs-reality: the Slack example's per-user identity path must work (#358).

`slack_tools._resolve_user_identity` claimed to pull the caller's Slack identity
from the request context, but it imported `apx_agent.get_request_context` — a
symbol that exists nowhere — and swallowed the ImportError with a bare
`except Exception`. So the documented per-user OBO path could never run: every
call silently fell through to the single shared `SLACK_USER_IDENTITY` env var
(or None), posting Slack messages as the wrong user for everyone with no error.

The fix reads the real per-request headers Databricks Apps inject
(`X-Forwarded-Email`) via mlflow's `get_request_headers` + apx's
`extract_obo_headers`. This reconciles the *claim* ("identity comes from the
request context") against reality: a served request with the user's email
header resolves to that email, and the dead symbol is gone.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

import pytest

_SLACK_TOOLS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "examples"
    / "slack-uc-mcp"
    / "slack_tools.py"
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("slack_tools_under_test", _SLACK_TOOLS)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
def test_resolves_caller_email_from_request_headers(monkeypatch) -> None:
    ags = pytest.importorskip("mlflow.genai.agent_server")
    monkeypatch.delenv("SLACK_USER_IDENTITY", raising=False)
    monkeypatch.setattr(
        ags, "get_request_headers", lambda: {"X-Forwarded-Email": "user@x.com"}, raising=False
    )
    mod = _load()
    assert mod._resolve_user_identity() == "user@x.com", (
        "per-user OBO identity did not resolve from the request headers "
        "(the dead get_request_context path)"
    )


@pytest.mark.unit
def test_falls_back_to_env_when_no_active_request(monkeypatch) -> None:
    ags = pytest.importorskip("mlflow.genai.agent_server")
    monkeypatch.setenv("SLACK_USER_IDENTITY", "dev-U1")
    monkeypatch.setattr(ags, "get_request_headers", lambda: None, raising=False)
    mod = _load()
    assert mod._resolve_user_identity() == "dev-U1"


@pytest.mark.unit
def test_no_dead_get_request_context_reference() -> None:
    # The nonexistent symbol whose ImportError was swallowed must be gone.
    assert "get_request_context" not in _SLACK_TOOLS.read_text()
