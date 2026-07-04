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


# ===========================================================================
# remove_from_registry / find_registry_dependents (#446)
# ===========================================================================


def _capture_run_sql(calls: list, rows_for: dict | None = None):
    """A run_sql stand-in that records SQL and serves canned SELECT rows.

    ``rows_for`` maps a substring of the SQL text to the row list to return.
    """

    def _fake(_ws, sql, warehouse_id=None, **_kw):
        calls.append(sql)
        for marker, rows in (rows_for or {}).items():
            if marker in sql:
                return rows
        return []

    return _fake


def test_remove_from_registry_deletes_both_tables() -> None:
    from apx_agent._publish import remove_from_registry

    calls: list[str] = []
    with patch("apx_agent._sql.run_sql", side_effect=_capture_run_sql(calls)):
        remove_from_registry(agent_id="victim_host", ws=MagicMock(), warehouse_id="wh1")

    deletes = [s for s in calls if "DELETE FROM" in s]
    assert len(deletes) == 2, f"expected registry + tools DELETE, got: {calls}"
    assert any(
        "main.apx.agent_registry" in s and "'victim_host'" in s for s in deletes
    ), deletes
    assert any(
        "main.apx.agent_tools" in s and "'victim_host'" in s for s in deletes
    ), deletes


def test_remove_from_registry_honours_table_overrides() -> None:
    from apx_agent._publish import remove_from_registry

    calls: list[str] = []
    with patch("apx_agent._sql.run_sql", side_effect=_capture_run_sql(calls)):
        remove_from_registry(
            agent_id="victim",
            registry_table="cat.sch.agents",
            tools_table="cat.sch.tools",
            ws=MagicMock(),
        )

    assert any("cat.sch.agents" in s for s in calls), calls
    assert any("cat.sch.tools" in s for s in calls), calls
    assert not any("main.apx" in s for s in calls), calls


def test_remove_from_registry_propagates_sql_failure() -> None:
    from apx_agent._publish import remove_from_registry

    with patch(
        "apx_agent._sql.run_sql", side_effect=RuntimeError("PERMISSION_DENIED")
    ):
        with pytest.raises(RuntimeError, match="PERMISSION_DENIED"):
            remove_from_registry(agent_id="victim", ws=MagicMock())


def test_remove_from_registry_rejects_malformed_table_name() -> None:
    from apx_agent._publish import remove_from_registry

    with patch("apx_agent._sql.run_sql") as run_sql:
        with pytest.raises(ValueError):
            remove_from_registry(
                agent_id="victim",
                registry_table="bad table; DROP",
                ws=MagicMock(),
            )
    run_sql.assert_not_called()


# ===========================================================================
# registry ownership gate (#464 — sub-agent spoofing)
# ===========================================================================


def _ws_as(principal: str) -> MagicMock:
    """A WorkspaceClient mock whose current user resolves to *principal*."""
    ws = MagicMock()
    ws.current_user.me.return_value = SimpleNamespace(
        user_name=principal, display_name=principal
    )
    ws.config.host = "https://ws.cloud.databricks.com"
    return ws


def test_publish_to_registry_blocks_foreign_owner() -> None:
    from apx_agent._publish import publish_to_registry

    calls: list[str] = []
    fake = _capture_run_sql(calls, rows_for={"SELECT published_by": [{"published_by": "alice@corp"}]})
    with patch("apx_agent._sql.run_sql", side_effect=fake):
        with pytest.raises(PermissionError, match="registered by 'alice@corp'"):
            publish_to_registry(name="shared", description="x", ws=_ws_as("bob@corp"))
    assert not any("MERGE INTO" in s for s in calls), "must not write after ownership denial"


def test_remove_from_registry_blocks_foreign_owner() -> None:
    from apx_agent._publish import remove_from_registry

    calls: list[str] = []
    fake = _capture_run_sql(calls, rows_for={"SELECT published_by": [{"published_by": "alice@corp"}]})
    with patch("apx_agent._sql.run_sql", side_effect=fake):
        with pytest.raises(PermissionError, match="may not overwrite or remove"):
            remove_from_registry(agent_id="shared_ws", ws=_ws_as("bob@corp"))
    assert not any("DELETE FROM" in s for s in calls), "must not delete after ownership denial"


def test_publish_to_registry_allows_same_owner() -> None:
    from apx_agent._publish import publish_to_registry

    calls: list[str] = []
    fake = _capture_run_sql(calls, rows_for={"SELECT published_by": [{"published_by": "alice@corp"}]})
    with patch("apx_agent._sql.run_sql", side_effect=fake):
        publish_to_registry(name="shared", description="x", ws=_ws_as("alice@corp"))
    assert any("MERGE INTO" in s for s in calls), "owner re-publish must proceed"


def test_publish_to_registry_allows_first_registration() -> None:
    from apx_agent._publish import publish_to_registry

    calls: list[str] = []
    # Empty owner SELECT → no existing row → first registration proceeds.
    with patch("apx_agent._sql.run_sql", side_effect=_capture_run_sql(calls)):
        publish_to_registry(name="fresh", description="x", ws=_ws_as("bob@corp"))
    assert any("MERGE INTO" in s for s in calls), "first registration must proceed"


def test_registry_owner_check_skips_on_select_error() -> None:
    """A missing SELECT grant must not block the delete — UC grants govern, so the
    ownership gate proceeds when it can't positively read a foreign owner."""
    from apx_agent._publish import remove_from_registry

    calls: list[str] = []

    def _raise_on_select(_ws, sql, warehouse_id=None, **_kw):
        calls.append(sql)
        if "SELECT" in sql:
            raise RuntimeError("no SELECT grant on registry")
        return []

    with patch("apx_agent._sql.run_sql", side_effect=_raise_on_select):
        remove_from_registry(agent_id="x", ws=_ws_as("bob@corp"))
    assert any("DELETE FROM" in s for s in calls), "delete proceeds despite SELECT failure"


def test_find_registry_dependents_merges_registry_and_tools_hits() -> None:
    from apx_agent._publish import find_registry_dependents

    calls: list[str] = []
    fake = _capture_run_sql(
        calls,
        rows_for={
            "agent_registry": [{
                "name": "shadow-advertise",
                "endpoint_url": "https://h/apps/my-app",
                "supervisor_agent_id": None,
                "updated_at": "100.0",
            }],
            "agent_tools": [{
                "agent_name": "billing-bot",
                "name": "ask_victim",
                "sub_agent_url": "https://h/apps/my-app",
                "updated_at": "200.0",
            }],
        },
    )
    with patch("apx_agent._sql.run_sql", side_effect=fake):
        dependents = find_registry_dependents(
            agent_ids=["victim_host"],
            references=["main.sch.victim", "my-ep", "my-app"],
            ws=MagicMock(),
        )

    assert [d["agent"] for d in dependents] == ["shadow-advertise", "billing-bot"]
    assert all(d["via"] == "https://h/apps/my-app" for d in dependents)
    assert [d["updated_at"] for d in dependents] == ["100.0", "200.0"]
    # The scan must scope out the victim's own rows and LIKE-match every
    # reference form (UC name, endpoint, app).
    selects = [s for s in calls if "SELECT" in s]
    assert len(selects) == 2
    for s in selects:
        assert "'victim_host'" in s and "NOT IN" in s, s
        assert "'%my-app%'" in s and "'%my-ep%'" in s and "'%main.sch.victim%'" in s, s


def test_find_registry_dependents_without_references_runs_no_sql() -> None:
    from apx_agent._publish import find_registry_dependents

    with patch("apx_agent._sql.run_sql") as run_sql:
        assert find_registry_dependents(
            agent_ids=["victim"], references=[], ws=MagicMock(),
        ) == []
        assert find_registry_dependents(
            agent_ids=[], references=["my-app"], ws=MagicMock(),
        ) == []
    run_sql.assert_not_called()
