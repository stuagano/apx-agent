"""The Knowledge page's empty state tells a missing bundle from one with nothing to show yet."""
from __future__ import annotations

from apx_agent._okf import write_okf_bundle
from apx_agent._ui_knowledge import render_knowledge_ui


def test_no_bundle_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "No OKF knowledge bundle found" in render_knowledge_ui()


def test_bare_bundle_points_to_grounding(tmp_path, monkeypatch):
    # A freshly generated bundle has tables but no descriptions, glossary or functions yet.
    write_okf_bundle({"catalog": "c", "schema": "s", "tables": {"orders": ["id(INT)"]}},
                     tmp_path / ".apx" / "okf", timestamp="z")
    monkeypatch.chdir(tmp_path)
    page = render_knowledge_ui()
    assert "No OKF knowledge bundle found" not in page
    assert "c.s" in page and "1 table" in page
    assert 'href="/_apx/grounding"' in page
