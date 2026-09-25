"""Test that trace pages render with Du Bois theme."""

import pytest
from httpx import ASGITransport, AsyncClient, Response


async def _get(app, path: str) -> Response:
    """Helper to make a GET request to the ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


@pytest.mark.asyncio
async def test_trace_list_uses_dubois(dev_ui_app):
    """Verify trace list page uses Du Bois theme (not legacy palette)."""
    r = await _get(dev_ui_app, "/_apx/traces")
    assert r.status_code == 200
    assert "apx-dubois-theme" in r.text, "Du Bois theme style block must be injected"

    # Verify legacy neutral palette is gone
    for legacy in ["#0a0a0a", "#e5e7eb", "#2a2a2a", "#888"]:
        assert legacy not in r.text, f"trace list still uses legacy {legacy}"

    # Verify Du Bois palette is present
    assert "#11171C" in r.text, "Du Bois bg not found"
    assert "#2272B4" in r.text, "Du Bois accent not found"


@pytest.mark.asyncio
async def test_trace_detail_uses_dubois(dev_ui_app):
    """Verify trace detail page uses Du Bois theme."""
    # Note: This will return an error (no trace_id exists), but we can check
    # the HTML it renders for the theme before the error happens in JS
    r = await _get(dev_ui_app, "/_apx/traces/test-trace-id")
    assert r.status_code == 200
    assert "apx-dubois-theme" in r.text, "Du Bois theme style block must be injected"

    # Verify legacy palette is gone
    for legacy in ["#0a0a0a", "#e5e7eb", "#2a2a2a", "#888"]:
        assert legacy not in r.text, f"trace detail still uses legacy {legacy}"

    # Verify Du Bois palette is present
    assert "#11171C" in r.text, "Du Bois bg not found"
    assert "#2272B4" in r.text, "Du Bois accent not found"


@pytest.mark.asyncio
async def test_trace_diff_uses_dubois(dev_ui_app):
    """Verify trace diff page uses Du Bois theme."""
    r = await _get(dev_ui_app, "/_apx/traces/diff?a=tid1&b=tid2")
    assert r.status_code == 200
    assert "apx-dubois-theme" in r.text, "Du Bois theme style block must be injected"

    # Verify legacy palette is gone
    for legacy in ["#0a0a0a", "#e5e7eb", "#2a2a2a", "#888"]:
        assert legacy not in r.text, f"trace diff still uses legacy {legacy}"

    # Verify Du Bois palette is present
    assert "#11171C" in r.text, "Du Bois bg not found"
    assert "#2272B4" in r.text, "Du Bois accent not found"


@pytest.mark.asyncio
async def test_semantic_span_type_hues_preserved(dev_ui_app):
    """Verify semantic span-type badge colors are kept distinct (not flattened)."""
    r = await _get(dev_ui_app, "/_apx/traces")
    assert r.status_code == 200

    # These semantic hues MUST be preserved (different colors for different meanings):
    # - Cyan for LLM
    assert "#22d3ee" in r.text, "LLM span-type cyan must be preserved"
    # - Yellow for TOOL
    assert "#facc15" in r.text, "TOOL span-type yellow must be preserved"
    # - Purple for CHAIN
    assert "#a78bfa" in r.text, "CHAIN span-type purple must be preserved"
    # - Blue (accent) for AGENT
    assert "#4299E0" in r.text or "#2272B4" in r.text, "AGENT span-type blue hue must be present"
    # - Slate/muted for OTHER
    assert "#92A4B3" in r.text, "OTHER span-type muted must be preserved"


@pytest.mark.asyncio
async def test_status_colors_preserved(dev_ui_app):
    """Verify status indicator colors are kept distinct."""
    r = await _get(dev_ui_app, "/_apx/traces")
    assert r.status_code == 200

    # Status colors MUST be preserved (different colors for different states):
    # - Green for ok
    assert "#3BA65E" in r.text, "OK status green must be preserved"
    # - Red for err
    assert "#C83243" in r.text, "ERR status red must be preserved"
    # - Yellow for run (in-progress)
    assert "#FACB66" in r.text, "RUN status yellow must be preserved"
