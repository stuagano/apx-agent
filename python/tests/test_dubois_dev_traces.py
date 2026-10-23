"""Test that trace pages render with AgentKit theme."""

import re

import pytest
from httpx import ASGITransport, AsyncClient, Response


async def _get(app, path: str) -> Response:
    """Helper to make a GET request to the ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


@pytest.mark.asyncio
async def test_trace_list_uses_dubois(dev_ui_app):
    """Verify trace list page uses AgentKit theme (not legacy palette)."""
    r = await _get(dev_ui_app, "/_apx/traces")
    assert r.status_code == 200
    assert "apx-agentkit-theme" in r.text, "AgentKit theme style block must be injected"

    # Verify legacy neutral palette is gone
    for legacy in ["#0a0a0a", "#e5e7eb", "#2a2a2a", "#888"]:
        assert legacy not in r.text, f"trace list still uses legacy {legacy}"

    # Verify AgentKit palette is present
    assert "#f1f3ff" in r.text, "AgentKit bg not found"
    assert "#1a73e8" in r.text, "AgentKit accent not found"


@pytest.mark.asyncio
async def test_trace_detail_uses_dubois(dev_ui_app):
    """Verify trace detail page uses AgentKit theme."""
    # Note: This will return an error (no trace_id exists), but we can check
    # the HTML it renders for the theme before the error happens in JS
    r = await _get(dev_ui_app, "/_apx/traces/test-trace-id")
    assert r.status_code == 200
    assert "apx-agentkit-theme" in r.text, "AgentKit theme style block must be injected"

    # Verify legacy palette is gone
    for legacy in ["#0a0a0a", "#e5e7eb", "#2a2a2a", "#888"]:
        assert legacy not in r.text, f"trace detail still uses legacy {legacy}"

    # Verify AgentKit palette is present
    assert "#f1f3ff" in r.text, "AgentKit bg not found"
    assert "#1a73e8" in r.text, "AgentKit accent not found"


@pytest.mark.asyncio
async def test_trace_diff_uses_dubois(dev_ui_app):
    """Verify trace diff page uses AgentKit theme."""
    r = await _get(dev_ui_app, "/_apx/traces/diff?a=tid1&b=tid2")
    assert r.status_code == 200
    assert "apx-agentkit-theme" in r.text, "AgentKit theme style block must be injected"

    # Verify legacy palette is gone
    for legacy in ["#0a0a0a", "#e5e7eb", "#2a2a2a", "#888"]:
        assert legacy not in r.text, f"trace diff still uses legacy {legacy}"

    # Verify AgentKit palette is present
    assert "#f1f3ff" in r.text, "AgentKit bg not found"
    assert "#1a73e8" in r.text, "AgentKit accent not found"


@pytest.mark.asyncio
async def test_semantic_span_type_hues_preserved(dev_ui_app):
    """Verify semantic span-type badge colors are kept distinct (not flattened)."""
    r = await _get(dev_ui_app, "/_apx/traces")
    assert r.status_code == 200

    # Semantic hues stay distinct per span type (tokens, defined in _ui_theme).
    hues = {}
    for kind in ("LLM", "TOOL", "CHAIN", "AGENT", "OTHER"):
        m = re.search(r"\.stype-%s\{color:var\((--apx-[\w-]+)\)" % kind, r.text)
        assert m, f"{kind} span type must use a theme token"
        hues[kind] = m.group(1)
    assert len(set(hues.values())) == 5, f"span-type hues must be distinct: {hues}"


@pytest.mark.asyncio
async def test_status_colors_preserved(dev_ui_app):
    """Verify status indicator colors are kept distinct."""
    r = await _get(dev_ui_app, "/_apx/traces")
    assert r.status_code == 200

    # Status colors stay distinct per state.
    for state, token in (("ok", "--apx-ok"), ("err", "--apx-err"), ("run", "--apx-warn")):
        assert f".st-{state}{{color:var({token});}}" in r.text, f"{state} status color"

