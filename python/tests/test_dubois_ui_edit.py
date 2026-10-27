"""Test that /_apx/edit page uses Du Bois design tokens instead of legacy hex colors."""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient


async def _get(app, path):
    """Fetch a path from the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


@pytest.mark.asyncio
async def test_edit_uses_dubois_tokens(dev_ui_app):
    """Verify /_apx/edit uses theme tokens, not legacy hex colors."""
    r = await _get(dev_ui_app, "/_apx/edit")
    assert r.status_code == 200
    assert "apx-dubois-theme" in r.text, "Missing theme marker"

    # Extract the main edit UI style block (between <title> and first <script>)
    # This isolates the edit UI from injected overlays defined in other modules
    match = re.search(
        r'<title>Edit — APX Dev</title>.*?<script type="module">',
        r.text,
        re.DOTALL
    )
    edit_section = match.group(0) if match else r.text

    # Legacy hex values that should NOT appear in the edit UI section
    legacy_hexes = [
        "#0d0d0d", "#0a0a0a", "#141414",  # bg
        "#111", "#222",  # panels
        "#1e3a5f", "#0d1f38", "#0c1a2e",  # panel-2/accent-bg
        "#2a2a2a", "#1e1e1e", "#333", "#444",  # borders
        "#e8e8e8", "#fff", "#ccc", "#cfe6ff",  # text
        "#888", "#555", "#666", "#777", "#aaa",  # muted
        "#60b0ff", "#3a7bd5", "#2563eb", "#1d4ed8", "#2d568a", "#285080", "#5a7fae", "#a5f3fc",  # accent/links
        "#4ade80", "#1a4a1a", "#0d2a0d",  # ok/success
        "#f87171",  # err
        "#ffb84d", "#5a3a00",  # warn
        "#9d7bff", "#1a1230", "#3a2d5f",  # purple accents
    ]

    for legacy_hex in legacy_hexes:
        assert legacy_hex.lower() not in edit_section.lower(), (
            f"/_apx/edit edit section still uses legacy hex {legacy_hex}"
        )
