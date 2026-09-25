"""Tests for Du Bois theme migration in _ui_tools.py."""

import pytest
from httpx import ASGITransport, AsyncClient


async def _get(app, path):
    """Helper to make async HTTP requests to an ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


@pytest.mark.asyncio
async def test_tools_uses_dubois_tokens(dev_ui_app):
    """Verify that the tools UI renders with Du Bois theme tokens injected."""
    r = await _get(dev_ui_app, "/_apx/tools")
    assert r.status_code == 200

    # Theme must be injected
    assert "apx-dubois-theme" in r.text, "theme not injected — check the .replace()"

    # Placeholder must not be left unsubstituted
    assert "{APX_THEME}" not in r.text, "placeholder left unsubstituted"

    # The :root must now carry Du Bois values, not legacy
    assert "#0a0a0a" not in r.text, "legacy bg value #0a0a0a remains"
    assert "#60b0ff" not in r.text, "legacy accent value #60b0ff remains"
    assert "#0d1f38" not in r.text, "legacy accent-bg value #0d1f38 remains"

    # Du Bois values must be present in :root or via apx_theme_style()
    assert "#11171C" in r.text, "Du Bois bg value #11171C missing"
    assert "#2272B4" in r.text, "Du Bois accent value #2272B4 missing"
    assert "#37444F" in r.text, "Du Bois accent-bg value #37444F missing"
