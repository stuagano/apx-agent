"""Test that _ui_chat.py uses Du Bois design tokens, not legacy hex colors."""

from __future__ import annotations

import re
import pytest
from httpx import ASGITransport, AsyncClient


async def _get(app, path):
    """Fetch a path from the app, following redirects."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


@pytest.mark.asyncio
async def test_chat_uses_dubois_tokens(dev_ui_app):
    """Verify /_apx/agent uses Du Bois theme and no legacy hex colors."""
    r = await _get(dev_ui_app, "/_apx/agent")
    assert r.status_code == 200

    # Theme marker must be present
    assert "apx-dubois-theme" in r.text

    # Legacy palette hex colors must be gone from the rendered page (check as complete hex values)
    hex_colors = re.findall(r'(?<![0-9a-fA-F])#[0-9a-fA-F]{3,6}(?![0-9a-fA-F])', r.text)
    legacy_hexes = {"#0a0a0a", "#60b0ff", "#e5e7eb", "#2a2a2a", "#111", "#0d1f38", "#888"}
    legacy_in_output = [h for h in hex_colors if h in legacy_hexes]
    assert not legacy_in_output, f"/_apx/agent still uses legacy hexes: {legacy_in_output}"

    # Token references should be present
    assert "var(--apx-" in r.text
