"""Tests for Du Bois design language migration of _ui_knowledge and _ui_root_chat."""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient, Response


async def _get(app, path: str) -> Response:
    """Helper to GET a path from the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get(path, follow_redirects=True)


@pytest.mark.asyncio
async def test_knowledge_uses_dubois_tokens(dev_ui_app):
    """Knowledge tab must use apx-dubois-theme and no legacy hex colors."""
    r = await _get(dev_ui_app, "/_apx/knowledge")
    assert r.status_code == 200
    assert "apx-dubois-theme" in r.text, "Knowledge page missing apx-dubois-theme marker"

    # Check for legacy hexes that should have been replaced with tokens (6-digit hex patterns)
    legacy_hexes = ["#0a0a0a", "#60b0ff", "#e5e7eb", "#2a2a2a", "#0d0d0d", "#0f0f0f", "#1a1a1a"]
    for legacy in legacy_hexes:
        # Use word boundary to avoid matching as substrings of longer hex values
        pattern = re.compile(r'\b' + re.escape(legacy) + r'\b')
        assert not pattern.search(r.text), f"Knowledge page still contains legacy hex {legacy}"


@pytest.mark.asyncio
async def test_knowledge_has_expected_tokens(dev_ui_app):
    """Knowledge page should reference apx-* CSS variables."""
    r = await _get(dev_ui_app, "/_apx/knowledge")
    assert r.status_code == 200

    # Spot-check expected token usage
    token_refs = [
        "var(--apx-bg)",
        "var(--apx-panel)",
        "var(--apx-border)",
        "var(--apx-text)",
        "var(--apx-muted)",
        "var(--apx-accent)",
    ]
    for token in token_refs:
        assert token in r.text, f"Knowledge page missing expected token {token}"


@pytest.mark.asyncio
async def test_root_chat_uses_dubois_tokens():
    """Root chat page (rendered directly) must use apx-dubois-theme and no legacy hex colors."""
    from apx_agent._ui_root_chat import render_root_chat

    html = render_root_chat(name="Test Agent", description="Test Description")
    assert "apx-dubois-theme" in html, "Root chat page missing apx-dubois-theme marker"

    # Check for legacy hexes that should have been replaced with tokens (6-digit hex patterns)
    legacy_hexes = ["#0a0a0a", "#60b0ff", "#e5e7eb", "#2a2a2a", "#0d1f38", "#1e3a5f"]
    for legacy in legacy_hexes:
        # Use word boundary to avoid matching as substrings of longer hex values
        pattern = re.compile(r'\b' + re.escape(legacy) + r'\b')
        assert not pattern.search(html), f"Root chat page still contains legacy hex {legacy}"


@pytest.mark.asyncio
async def test_root_chat_has_expected_tokens():
    """Root chat page should reference apx-* CSS variables."""
    from apx_agent._ui_root_chat import render_root_chat

    html = render_root_chat(name="Test Agent")

    # Spot-check expected token usage
    token_refs = [
        "var(--apx-bg)",
        "var(--apx-panel)",
        "var(--apx-border)",
        "var(--apx-text)",
        "var(--apx-muted)",
        "var(--apx-accent)",
    ]
    for token in token_refs:
        assert token in html, f"Root chat page missing expected token {token}"


@pytest.mark.asyncio
async def test_root_chat_escapes_title_and_description():
    """Root chat must safely escape the title and description."""
    from apx_agent._ui_root_chat import render_root_chat

    html = render_root_chat(name="<script>alert('xss')</script>", description="Test <b>desc</b>")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html
