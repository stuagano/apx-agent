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
