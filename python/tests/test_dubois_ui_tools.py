"""Tests for AgentKit theme migration in _ui_tools.py."""

import pytest
from httpx import ASGITransport, AsyncClient


async def _get(app, path):
    """Helper to make async HTTP requests to an ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


@pytest.mark.asyncio
async def test_tools_uses_dubois_tokens(dev_ui_app):
    """Verify that the tools UI renders with AgentKit theme tokens injected."""
    r = await _get(dev_ui_app, "/_apx/tools")
    assert r.status_code == 200

    # Theme must be injected
    assert "apx-agentkit-theme" in r.text, "theme not injected — check the .replace()"

    # Placeholder must not be left unsubstituted
    assert "{APX_THEME}" not in r.text, "placeholder left unsubstituted"

    # The :root must now carry AgentKit values, not legacy
    assert "#0a0a0a" not in r.text, "legacy bg value #0a0a0a remains"
    assert "#60b0ff" not in r.text, "legacy accent value #60b0ff remains"
    assert "#0d1f38" not in r.text, "legacy accent-bg value #0d1f38 remains"

    # AgentKit values must be present in :root or via apx_theme_style()
    assert "#f1f3ff" in r.text, "AgentKit bg value #f1f3ff missing"
    assert "#1a73e8" in r.text, "AgentKit accent value #1a73e8 missing"
    assert "#37444F" in r.text, "AgentKit accent-bg value #37444F missing"
