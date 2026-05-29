"""Tests for _doctor.py — the apx environment diagnostic layer."""

from __future__ import annotations

from pathlib import Path

from apx_agent._doctor import Check, Status, run_checks


def test_check_is_frozen_dataclass():
    c = Check(name="X", status=Status.OK, detail="fine", fix=None)
    assert c.name == "X"
    assert c.status is Status.OK
    assert c.fix is None


def test_run_checks_returns_ordered_groups(tmp_path: Path):
    groups = run_checks(tmp_path, online=False)
    names = [g for g, _ in groups]
    assert names == ["Environment", "Authentication", "Project"]
    for _group, checks in groups:
        assert all(isinstance(c, Check) for c in checks)


import sys

import apx_agent._doctor as doctor


def test_python_version_ok(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 12, 2, "final", 0))
    c = doctor.check_python_version()
    assert c.status is doctor.Status.OK
    assert "3.12.2" in c.detail


def test_python_version_too_old(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 10, 9, "final", 0))
    c = doctor.check_python_version()
    assert c.status is doctor.Status.FAIL
    assert "3.11" in c.fix


def test_uv_present(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/uv")
    c = doctor.check_uv()
    assert c.status is doctor.Status.OK


def test_uv_missing_is_warn(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_uv()
    assert c.status is doctor.Status.WARN
    assert c.fix is not None


def test_databricks_cli_missing_is_warn(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_databricks_cli()
    assert c.status is doctor.Status.WARN
    assert "deploy" in c.detail


def test_uvicorn_present():
    # uvicorn is a dev/runtime dep installed in the test env.
    c = doctor.check_uvicorn()
    assert c.status in (doctor.Status.OK, doctor.Status.WARN)


from unittest.mock import MagicMock, patch


def test_auth_ok(monkeypatch):
    with patch("databricks.sdk.core.Config", return_value=object()):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.OK


def test_auth_no_profiles_first_timer(monkeypatch):
    def boom(*a, **k):
        raise ValueError("no creds")

    with patch("databricks.sdk.core.Config", side_effect=boom), patch(
        "apx_agent.cli._databrickscfg_profiles", return_value=[]
    ):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.FAIL
    assert "auth login" in c.fix


def test_auth_ambiguous_profiles(monkeypatch):
    def boom(*a, **k):
        raise ValueError("ambiguous")

    with patch("databricks.sdk.core.Config", side_effect=boom), patch(
        "apx_agent.cli._databrickscfg_profiles", return_value=["DEFAULT", "prod"]
    ):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.FAIL
    assert "DATABRICKS_CONFIG_PROFILE" in c.fix
    assert "prod" in c.fix


def test_workspace_skipped_when_auth_failed():
    c = doctor.check_databricks_workspace(auth_ok=False)
    assert c.status is doctor.Status.SKIP


def test_workspace_ok():
    me = MagicMock()
    me.user_name = "alice@example.com"
    client = MagicMock()
    client.current_user.me.return_value = me
    client.config.host = "https://x.cloud.databricks.com"
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.OK
    assert "alice@example.com" in c.detail


def test_workspace_expired_token():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("401 invalid access token")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "auth login" in c.fix


def test_workspace_unreachable():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("Name or service not known")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "host" in c.fix.lower()


def test_workspace_forbidden():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("403 PERMISSION_DENIED")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "permission" in c.detail.lower() or "403" in c.detail
