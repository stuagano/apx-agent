"""Discovery of apx agents deployed as Databricks Apps (probe the A2A card).

The probe itself is a network call, so these stub it: the unit under test is the
list/filter/merge logic, not urllib.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from apx_agent import _apps_discovery
from apx_agent._apps_discovery import AppAgentInfo, discover_app_agents
from apx_agent.cli import main


def _app(name: str, url: str | None, state: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name, url=url,
        app_status=SimpleNamespace(state=SimpleNamespace(value=state)),
    )


def _ws(apps: list) -> SimpleNamespace:
    return SimpleNamespace(
        apps=SimpleNamespace(list=lambda: apps),
        config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer x"}),
    )


CARD = {"name": "weather", "description": "weather agent",
        "skills": [{"id": "get_weather"}, {"id": "forecast"}]}


def test_only_apx_apps_are_returned(monkeypatch):
    # The probe is the identity+liveness filter: only apps that answer with a
    # card are kept. Apps without a URL are skipped outright.
    apps = [
        _app("weather-app", "https://weather.example", "RUNNING"),   # apx → kept
        _app("random-app", "https://random.example", "RUNNING"),     # not apx → dropped
        _app("dead-app", "https://dead.example", "STOPPED"),         # no card → dropped
        _app("no-url", None, "RUNNING"),                             # skipped (no url)
    ]
    def fake_probe(url, headers, timeout):
        return CARD if url == "https://weather.example" else None
    monkeypatch.setattr(_apps_discovery, "_probe_card", fake_probe)

    found = discover_app_agents(_ws(apps))
    assert [a.app_name for a in found] == ["weather-app"]
    a = found[0]
    assert a.name == "weather"
    assert a.url == "https://weather.example"
    assert a.tool_count == 2
    assert a.state == "RUNNING"


def test_apps_without_url_are_skipped(monkeypatch):
    probed: list[str] = []
    def fake_probe(url, headers, timeout):
        probed.append(url)
        return None
    monkeypatch.setattr(_apps_discovery, "_probe_card", fake_probe)

    discover_app_agents(_ws([_app("a", None, "RUNNING")]))
    assert probed == [], "apps without a URL must not be probed"


def test_apps_list_failure_is_non_fatal():
    ws = SimpleNamespace(
        apps=SimpleNamespace(list=lambda: (_ for _ in ()).throw(RuntimeError("denied"))),
        config=SimpleNamespace(authenticate=lambda: {}),
    )
    assert discover_app_agents(ws) == []


def test_list_merges_app_agent_as_its_own_row(monkeypatch):
    # No UC agents; one app-discovered agent → it shows up in `list` with
    # serving=apps and its url, not a UC name.
    monkeypatch.setattr("apx_agent.cli._require_sdk", lambda profile: object())
    monkeypatch.setattr("apx_agent.cli._fleet_resolve", lambda *a, **k: [])
    monkeypatch.setattr(
        "apx_agent._apps_discovery.discover_app_agents",
        lambda ws, **k: [AppAgentInfo(
            name="weather", app_name="weather-app", url="https://weather.example",
            description="d", tool_count=2, state="RUNNING")],
    )
    result = CliRunner().invoke(main, ["agents", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["serving"] == "apps"
    assert rows[0]["url"] == "https://weather.example"
    assert rows[0]["uc_name"] is None
    assert rows[0]["tool_count"] == 2


def test_list_dedups_uc_manifest_with_live_app(monkeypatch):
    # A UC apps-manifest row whose app_name matches a probed app → one merged
    # row carrying both the uc_name and the live url (not two rows).
    uc_agent = SimpleNamespace(
        name="weather", uc_name="cat.sch.weather", model=None,
        app_name="weather-app",
        tags={"apx.serving": "apps", "apx.agent.tool_count": "2"},
    )
    monkeypatch.setattr("apx_agent.cli._require_sdk", lambda profile: object())
    monkeypatch.setattr("apx_agent.cli._fleet_resolve", lambda *a, **k: [uc_agent])
    monkeypatch.setattr(
        "apx_agent._apps_discovery.discover_app_agents",
        lambda ws, **k: [AppAgentInfo(
            name="weather", app_name="weather-app", url="https://weather.example",
            description="d", tool_count=2, state="RUNNING")],
    )
    result = CliRunner().invoke(main, ["agents", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1, "matching app_name must merge, not duplicate"
    assert rows[0]["uc_name"] == "cat.sch.weather"
    assert rows[0]["url"] == "https://weather.example"
    assert rows[0]["serving"] == "apps"


def test_apps_only_skips_the_uc_scan(monkeypatch):
    # --apps-only must NOT call the UC resolver; only app agents are shown.
    def _boom(*a, **k):
        raise AssertionError("_fleet_resolve must not run under --apps-only")
    monkeypatch.setattr("apx_agent.cli._require_sdk", lambda profile: object())
    monkeypatch.setattr("apx_agent.cli._fleet_resolve", _boom)
    monkeypatch.setattr(
        "apx_agent._apps_discovery.discover_app_agents",
        lambda ws, **k: [AppAgentInfo(
            name="weather", app_name="weather-app", url="https://weather.example",
            description="d", tool_count=2, state="RUNNING")],
    )
    result = CliRunner().invoke(main, ["agents", "list", "--apps-only", "--format", "json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [r["agent_name"] for r in rows] == ["weather"]
    assert rows[0]["serving"] == "apps"


def test_apps_only_rejects_catalog_scope():
    result = CliRunner().invoke(main, ["agents", "list", "--apps-only", "--catalog", "main"])
    assert result.exit_code != 0
    assert "apps-only" in result.output.lower()
