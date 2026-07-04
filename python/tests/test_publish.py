"""Tests for _publish.py — Supervisor Agent publishing.

Covers:
  1. _slug normalises arbitrary strings into safe tool_id values.
  2. publish_to_supervisor builds a Tool with tool_type="serving_endpoint"
     and calls ws.supervisor_agents.create_tool with the correct parent
     and tool_id.
  3. tool_id defaults to a slug of the serving_endpoint name; explicit
     tool_id wins when supplied.
  4. extra_tool_kwargs merges into the Tool constructor (escape hatch
     for SDK evolution).
  5. create_supervisor_agent calls ws.supervisor_agents.create_supervisor_agent
     with a SupervisorAgent dataclass populated from kwargs.
  6. Friendly ImportError when supervisoragents SDK isn't available.
  7. app_name publishes a tool_type="app" Tool wrapping App(name=...);
     exactly-one-of serving_endpoint/app_name is enforced with a clear
     ValueError; an SDK without the App tool type raises a clear
     ImportError naming the databricks-sdk>=0.120 requirement (#444).

Uses a fake supervisoragents module + MagicMock WorkspaceClient.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apx_agent._publish import _slug


# ===========================================================================
# _slug
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("data-triage", "data_triage"),
        ("Data Triage", "data_triage"),
        ("a.b.c", "a_b_c"),
        ("___leading___", "leading"),
        ("__", "tool"),  # fallback for empty result
        ("Foo-Bar 99", "foo_bar_99"),
    ],
)
def test_slug_normalises(raw: str, expected: str) -> None:
    assert _slug(raw) == expected


# ===========================================================================
# Fake supervisoragents SDK module
# ===========================================================================


class _FakeTool:
    """Stand-in for databricks.sdk.service.supervisoragents.Tool."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeTool) and self.kwargs == other.kwargs


class _FakeSupervisorAgent:
    """Stand-in for SupervisorAgent dataclass."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class _FakeApp:
    """Stand-in for databricks.sdk.service.supervisoragents.App."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeApp) and self.name == other.name


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch, *, with_app: bool = True,
) -> None:
    """Inject a fake supervisoragents module so the lazy import resolves.

    ``with_app=False`` mimics an SDK release whose supervisoragents surface
    predates the App tool type (< 0.120).
    """
    fake_module = SimpleNamespace(
        Tool=_FakeTool,
        SupervisorAgent=_FakeSupervisorAgent,
    )
    if with_app:
        fake_module.App = _FakeApp
    monkeypatch.setitem(
        sys.modules,
        "databricks.sdk.service.supervisoragents",
        fake_module,
    )


# ===========================================================================
# publish_to_supervisor
# ===========================================================================


def test_publish_creates_tool_with_serving_endpoint_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch)
    from apx_agent import publish_to_supervisor

    ws = MagicMock()
    publish_to_supervisor(
        supervisor_agent_id="sa-123",
        serving_endpoint="data-triage",
        description="Triages missing-data questions.",
        ws=ws,
    )

    ws.supervisor_agents.create_tool.assert_called_once()
    call = ws.supervisor_agents.create_tool.call_args
    assert call.kwargs["parent"] == "supervisor-agents/sa-123"
    assert call.kwargs["tool_id"] == "data_triage"
    tool = call.kwargs["tool"]
    assert isinstance(tool, _FakeTool)
    assert tool.kwargs["tool_type"] == "serving_endpoint"
    assert tool.kwargs["name"] == "data-triage"
    assert tool.kwargs["description"] == "Triages missing-data questions."


def test_publish_explicit_tool_id_overrides_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch)
    from apx_agent import publish_to_supervisor

    ws = MagicMock()
    publish_to_supervisor(
        supervisor_agent_id="sa-123",
        serving_endpoint="data-triage",
        description="...",
        tool_id="data_triage_v2",
        ws=ws,
    )

    assert ws.supervisor_agents.create_tool.call_args.kwargs["tool_id"] == "data_triage_v2"


def test_publish_extra_tool_kwargs_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch)
    from apx_agent import publish_to_supervisor

    ws = MagicMock()
    publish_to_supervisor(
        supervisor_agent_id="sa-123",
        serving_endpoint="data-triage",
        description="...",
        ws=ws,
        extra_tool_kwargs={"id": "preview-only-field", "future_field": True},
    )

    tool = ws.supervisor_agents.create_tool.call_args.kwargs["tool"]
    assert tool.kwargs["id"] == "preview-only-field"
    assert tool.kwargs["future_field"] is True
    # Core fields still set
    assert tool.kwargs["tool_type"] == "serving_endpoint"


def test_publish_default_ws_constructed_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch)
    from apx_agent import publish_to_supervisor

    fake_ws_class = MagicMock()
    fake_ws_instance = MagicMock()
    fake_ws_class.return_value = fake_ws_instance

    with patch("databricks.sdk.WorkspaceClient", fake_ws_class):
        publish_to_supervisor(
            supervisor_agent_id="sa-1",
            serving_endpoint="agent-x",
            description="...",
            # ws omitted — should default-construct
        )

    fake_ws_class.assert_called_once()
    fake_ws_instance.supervisor_agents.create_tool.assert_called_once()


# ===========================================================================
# publish_to_supervisor — Databricks App target (#444)
# ===========================================================================


def test_publish_app_creates_app_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch)
    from apx_agent import publish_to_supervisor

    ws = MagicMock()
    publish_to_supervisor(
        supervisor_agent_id="sa-123",
        app_name="payroll-coworker",
        description="Answers payroll questions.",
        ws=ws,
    )

    ws.supervisor_agents.create_tool.assert_called_once()
    call = ws.supervisor_agents.create_tool.call_args
    assert call.kwargs["parent"] == "supervisor-agents/sa-123"
    assert call.kwargs["tool_id"] == "payroll_coworker"
    tool = call.kwargs["tool"]
    assert isinstance(tool, _FakeTool)
    assert tool.kwargs["tool_type"] == "app"
    assert tool.kwargs["app"] == _FakeApp(name="payroll-coworker")
    assert tool.kwargs["description"] == "Answers payroll questions."
    # The App reference carries the app name; the serving-endpoint "name"
    # field must NOT leak into an app tool.
    assert "name" not in tool.kwargs


def test_publish_app_explicit_tool_id_overrides_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_sdk(monkeypatch)
    from apx_agent import publish_to_supervisor

    ws = MagicMock()
    publish_to_supervisor(
        supervisor_agent_id="sa-123",
        app_name="payroll-coworker",
        description="...",
        tool_id="payroll_v2",
        ws=ws,
    )

    assert ws.supervisor_agents.create_tool.call_args.kwargs["tool_id"] == "payroll_v2"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # neither target
        {"serving_endpoint": "data-triage", "app_name": "payroll-coworker"},  # both
    ],
)
def test_publish_requires_exactly_one_target(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict,
) -> None:
    _install_fake_sdk(monkeypatch)
    from apx_agent import publish_to_supervisor

    ws = MagicMock()
    with pytest.raises(ValueError, match="exactly one of serving_endpoint"):
        publish_to_supervisor(
            supervisor_agent_id="sa-1", description="...", ws=ws, **kwargs,
        )
    ws.supervisor_agents.create_tool.assert_not_called()


def test_publish_app_clear_error_when_sdk_lacks_app_tool_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK with supervisoragents but no App type fails loud, not opaque."""
    _install_fake_sdk(monkeypatch, with_app=False)
    from apx_agent import publish_to_supervisor

    ws = MagicMock()
    with pytest.raises(ImportError, match=r"databricks-sdk>=0\.120"):
        publish_to_supervisor(
            supervisor_agent_id="sa-1",
            app_name="payroll-coworker",
            description="...",
            ws=ws,
        )
    ws.supervisor_agents.create_tool.assert_not_called()


def test_publish_endpoint_path_unchanged_when_app_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding the app path must not disturb the serving-endpoint request shape."""
    _install_fake_sdk(monkeypatch)
    from apx_agent import publish_to_supervisor

    ws = MagicMock()
    publish_to_supervisor(
        supervisor_agent_id="sa-123",
        serving_endpoint="data-triage",
        description="Triages missing-data questions.",
        ws=ws,
    )

    tool = ws.supervisor_agents.create_tool.call_args.kwargs["tool"]
    assert tool.kwargs == {
        "tool_type": "serving_endpoint",
        "name": "data-triage",
        "description": "Triages missing-data questions.",
    }


# ===========================================================================
# create_supervisor_agent
# ===========================================================================


def test_create_supervisor_agent_passes_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sdk(monkeypatch)
    from apx_agent import create_supervisor_agent

    ws = MagicMock()
    create_supervisor_agent(
        display_name="Data Ops Supervisor",
        description="Routes data-team queries.",
        instructions="Pick the right specialist.",
        ws=ws,
    )

    ws.supervisor_agents.create_supervisor_agent.assert_called_once()
    arg = ws.supervisor_agents.create_supervisor_agent.call_args.kwargs["supervisor_agent"]
    assert isinstance(arg, _FakeSupervisorAgent)
    assert arg.kwargs["display_name"] == "Data Ops Supervisor"
    assert arg.kwargs["description"] == "Routes data-team queries."
    assert arg.kwargs["instructions"] == "Pick the right specialist."


# ===========================================================================
# Friendly ImportError when SDK module is missing
# ===========================================================================


def test_friendly_error_when_supervisor_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force the import to fail
    monkeypatch.setitem(sys.modules, "databricks.sdk.service.supervisoragents", None)
    from apx_agent import publish_to_supervisor

    with pytest.raises(ImportError, match="supervisoragents service"):
        publish_to_supervisor(
            supervisor_agent_id="sa-1",
            serving_endpoint="agent-x",
            description="...",
            ws=MagicMock(),
        )
