"""Du Bois theme migration test for _ui_grounding.py.

Verify that the grounding UI uses the shared Du Bois design tokens instead of
hardcoded hex values, and that the theme style is injected on page render.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


async def _get(app, path):
    """Helper to make async HTTP requests to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


class TestGroundingUsesDuBoisTheme:
    """The grounding UI renders with Du Bois design tokens, not legacy hex values."""

    @pytest.mark.asyncio
    async def test_grounding_uses_dubois_tokens(self, dev_ui_app):
        """The /_apx/grounding page includes the Du Bois theme style block
        and uses var(--apx-*) tokens instead of hardcoded hex values."""
        r = await _get(dev_ui_app, "/_apx/grounding")
        assert r.status_code == 200
        # Theme marker is present in the rendered HTML
        assert "apx-dubois-theme" in r.text
        # Legacy hex values are gone
        legacy_hexes = [
            "#0a0a0a",  # old bg
            "#0d0d0d",  # old bg
            "#0d1f38",  # old nav active bg
            "#0d2818",  # old accept bg (off)
            "#11401f",  # old accept bg (hover)
            "#1a1040",  # old suggest bg (off)
            "#1a1a1a",  # old panel
            "#1d4ed8",  # old button hover
            "#1d5a3a",  # old accept border (off)
            "#1e3a5f",  # old badge bg
            "#2563eb",  # old button bg
            "#2a2a2a",  # old border/nav hover border
            "#2d1b69",  # old suggest hover
            "#3a7bd5",  # old textarea focus
            "#4ade80",  # old ok/accept text
            "#4c1d95",  # old suggest border (off)
            "#60b0ff",  # old accent/badge text
            "#a78bfa",  # old suggest text
            "#ccc",     # old text variants
            "#e8e8e8",  # old text
            "#f87171",  # old error
        ]
        for legacy in legacy_hexes:
            assert legacy not in r.text, (
                f"still uses legacy hex {legacy} — should use Du Bois token instead"
            )

    @pytest.mark.asyncio
    async def test_grounding_theme_block_contains_apx_vars(self, dev_ui_app):
        """The injected theme style includes the Du Bois CSS variables."""
        r = await _get(dev_ui_app, "/_apx/grounding")
        assert r.status_code == 200
        # Key Du Bois tokens are defined in the theme block
        tokens = [
            "--apx-bg",
            "--apx-panel",
            "--apx-border",
            "--apx-text",
            "--apx-muted",
            "--apx-accent",
            "--apx-ok",
            "--apx-err",
        ]
        for token in tokens:
            assert token in r.text, (
                f"Du Bois token {token} not found in theme style"
            )

    @pytest.mark.asyncio
    async def test_grounding_page_specific_classes_aliased_with_tokens(self, dev_ui_app):
        """Page-specific classes like .suggest and .accept use Du Bois tokens,
        not hardcoded colors. JS class refs are kept (not renamed)."""
        r = await _get(dev_ui_app, "/_apx/grounding")
        assert r.status_code == 200
        # JS-referenced classes are still present by name
        assert 'class="suggest"' in r.text
        assert 'class="accept"' in r.text
        # But they're aliased to tokens in the CSS, not hardcoded hex
        # e.g. .suggest { background: var(--apx-panel-2); ... }
        assert "var(--apx-panel-2)" in r.text
        assert "var(--apx-accent)" in r.text
        assert "var(--apx-ok)" in r.text

    @pytest.mark.asyncio
    async def test_grounding_html_renders_without_error(self, dev_ui_app):
        """The grounding page renders successfully with theme injection."""
        r = await _get(dev_ui_app, "/_apx/grounding")
        assert r.status_code == 200
        assert r.text.strip()  # Non-empty body
        assert "<title>Grounding" in r.text
        assert "</html>" in r.text  # Valid HTML structure
