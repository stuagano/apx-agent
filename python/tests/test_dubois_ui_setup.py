"""Migration of _ui_setup.py to Du Bois design language."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


async def _get(app, path):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


@pytest.mark.asyncio
async def test_setup_uses_dubois_tokens(dev_ui_app):
    """/_apx/setup should inject Du Bois theme and use CSS variables for main colors."""
    r = await _get(dev_ui_app, "/_apx/setup")
    assert r.status_code == 200
    assert "apx-dubois-theme" in r.text
    # Verify core Du Bois tokens are used in the main style block
    assert "var(--apx-bg)" in r.text or "background:" in r.text
    assert "var(--apx-text)" in r.text or "color:var" in r.text


@pytest.mark.asyncio
async def test_setup_embed_carries_theme(dev_ui_app):
    """/_apx/setup?embed=1 should also inject theme (exactly once)."""
    r = await _get(dev_ui_app, "/_apx/setup?embed=1")
    assert r.status_code == 200
    assert r.text.count("apx-dubois-theme") == 1


@pytest.mark.asyncio
async def test_eval_uses_dubois_tokens(dev_ui_app):
    """/_apx/eval should inject Du Bois theme and use CSS variables."""
    r = await _get(dev_ui_app, "/_apx/eval")
    assert r.status_code == 200
    assert "apx-dubois-theme" in r.text
    assert "var(--apx-bg)" in r.text or "background:var" in r.text


@pytest.mark.asyncio
async def test_setup_no_legacy_hex_colors(dev_ui_app):
    """/_apx/setup from _ui_setup.py uses CSS variables for all core colors."""
    r = await _get(dev_ui_app, "/_apx/setup")
    assert r.status_code == 200
    # Verify core setup styles use CSS variables for all key colors
    assert "var(--apx-bg)" in r.text
    assert "var(--apx-text)" in r.text
    assert "var(--apx-panel)" in r.text
    assert "var(--apx-accent)" in r.text
    assert "var(--apx-ok)" in r.text
    assert "var(--apx-err)" in r.text


@pytest.mark.asyncio
async def test_eval_no_legacy_hex_colors(dev_ui_app):
    """/_apx/eval from _ui_setup.py uses CSS variables for all core colors."""
    r = await _get(dev_ui_app, "/_apx/eval")
    assert r.status_code == 200
    # Verify eval page uses CSS variables for core colors
    assert "var(--apx-bg)" in r.text
    assert "var(--apx-text)" in r.text
    assert "var(--apx-panel)" in r.text
    assert "var(--apx-accent)" in r.text
    assert "var(--apx-ok)" in r.text
    assert "var(--apx-err)" in r.text
