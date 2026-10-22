"""Vendored AgentKit chat app (databricks-agentbricks 0.5.0, LangGraph UI)."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "apx_agent"
ASSETS = SRC / "_static" / "agentkit_chat"
TEXT_FILES = ("index.html", "app.js", "styles.css")


def test_vendored_assets_present_and_non_empty() -> None:
    for name in (*TEXT_FILES, "markdown-it-15.0.2.min.js", "agentbricks-mark.png", "VENDORED.md"):
        path = ASSETS / name
        assert path.is_file() and path.stat().st_size > 100, name


def test_vendored_paths_are_rewritten_under_apx() -> None:
    for name in TEXT_FILES:
        text = (ASSETS / name).read_text(encoding="utf-8")
        leftovers = re.findall(r"""["'`(]/(?:ui-assets|api/demo|api/ui)\b""", text)
        assert leftovers == [], f"{name}: unrewritten AgentKit paths {leftovers}"
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "/_apx/assets/agentkit/app.js" in html
    assert "/_apx/api/demo/" in js and "/_apx/api/ui/config" in js
    # The invocation API stays where DurableAgentServer serves it.
    assert '"/api/invocations"' in js
