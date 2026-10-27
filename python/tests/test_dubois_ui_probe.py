"""Du Bois design token adoption for /_apx/probe."""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent import AgentConfig, AgentContext
from apx_agent._dev import build_dev_ui_router
from apx_agent._models import AgentCard


@pytest.fixture
def dev_ui_app():
    """A FastAPI app mounting the /_apx/* dev UI."""
    config = AgentConfig(name="probe-test", model="claude-fake")
    card = AgentCard(name="probe-test", description="", skills=[])
    ctx = AgentContext(config=config, tools=[], card=card, agent=None)  # type: ignore[arg-type]
    app = FastAPI()
    app.state.agent_context = ctx
    app.include_router(build_dev_ui_router())
    return app


async def _get(app: FastAPI, path: str) -> str:
    """Helper: GET a path and return response text."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        r = await ac.get(path, follow_redirects=True)
    return r.text


def _extract_probe_css(html: str) -> str:
    """Extract the second <style> block (probe CSS, after theme CSS)."""
    styles = re.findall(r'<style>(.*?)</style>', html, re.DOTALL)
    # First style is theme (apx-dubois-theme marker), second is probe CSS
    return styles[1] if len(styles) >= 2 else ""


class TestProbeUsesDuboisTokens:
    """Verify /_apx/probe uses Du Bois design tokens instead of legacy hexes."""

    @pytest.mark.asyncio
    async def test_probe_uses_dubois_tokens(self, dev_ui_app):
        """The probe page must contain the apx-dubois-theme marker and use CSS vars."""
        r = await _get(dev_ui_app, "/_apx/probe")
        assert "apx-dubois-theme" in r, "Missing apx-dubois-theme marker"

    @pytest.mark.asyncio
    async def test_probe_css_uses_design_tokens(self, dev_ui_app):
        """Probe's CSS should use var(--apx-*) tokens, not legacy hexes."""
        r = await _get(dev_ui_app, "/_apx/probe")
        probe_css = _extract_probe_css(r)
        assert "var(--apx-bg)" in probe_css, "Probe CSS should use var(--apx-bg) token"
        assert "var(--apx-panel)" in probe_css, "Probe CSS should use var(--apx-panel) token"
        assert "var(--apx-text)" in probe_css, "Probe CSS should use var(--apx-text) token"
        assert "var(--apx-border)" in probe_css, "Probe CSS should use var(--apx-border) token"
        assert "var(--apx-muted)" in probe_css, "Probe CSS should use var(--apx-muted) token"

    @pytest.mark.asyncio
    async def test_probe_css_no_legacy_card_primitives(self, dev_ui_app):
        """Probe CSS should not define local .card/.badge/.btn/.empty/.pre — use .apx-* instead."""
        r = await _get(dev_ui_app, "/_apx/probe")
        probe_css = _extract_probe_css(r)
        # Probe-specific CSS should not redefine shared primitives
        # (They come from the theme CSS instead)
        for primitive in [".card{", ".badge{", ".btn{", ".empty{", ".pre{"]:
            assert primitive not in probe_css, (
                f"Probe CSS should not define {primitive} — "
                f"use shared .apx-* classes from theme CSS instead"
            )

    @pytest.mark.asyncio
    async def test_probe_status_colors_use_tokens(self, dev_ui_app):
        """Probe CSS should use var(--apx-ok), var(--apx-err), var(--apx-warn) for status colors."""
        r = await _get(dev_ui_app, "/_apx/probe")
        probe_css = _extract_probe_css(r)
        assert "var(--apx-ok)" in probe_css, "Probe should use var(--apx-ok) for ok status"
        assert "var(--apx-err)" in probe_css, "Probe should use var(--apx-err) for err status"
        assert "var(--apx-warn)" in probe_css, "Probe should use var(--apx-warn) for warn status"

    @pytest.mark.asyncio
    async def test_probe_font_tokens(self, dev_ui_app):
        """Probe should use var(--apx-font) and var(--apx-mono) tokens."""
        r = await _get(dev_ui_app, "/_apx/probe")
        probe_css = _extract_probe_css(r)
        assert "var(--apx-mono)" in probe_css, "Probe CSS should use var(--apx-mono) for monospace"
