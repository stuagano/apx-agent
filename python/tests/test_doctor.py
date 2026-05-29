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
