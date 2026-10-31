"""Eval cases live beside the agent module, or at the project root when no module is loaded."""
from __future__ import annotations

from unittest.mock import patch

from apx_agent import _ui_edit


def test_evals_beside_the_agent_module(tmp_path):
    with patch.object(_ui_edit, "_find_agent_router_path", return_value=tmp_path / "agent.py"):
        assert _ui_edit._find_evals_path() == tmp_path / "evals.json"


def test_evals_at_the_project_root_without_an_agent_module(tmp_path, monkeypatch):
    # A host started by its own launcher has no importable `agent` module.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / "pkg").mkdir()
    monkeypatch.chdir(tmp_path / "pkg")
    with patch.object(_ui_edit, "_find_agent_router_path", return_value=None):
        assert _ui_edit._find_evals_path() == tmp_path / "evals.json"


def test_no_project_no_evals(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(_ui_edit, "_find_agent_router_path", return_value=None):
        assert _ui_edit._find_evals_path() is None
