"""AC-4 gate — render_pdf produces a valid PDF and is registered.

Resolves ``render_pdf`` from ``_tool_config._registry()`` (the literal key that
``[[tool.apx.tools]]`` uses), invokes the tool on a markdown brief, and uses
``ctk.verify(Artifact(...))`` to prove the output file is non-empty (> 1KB) and
starts with the ``%PDF`` signature — engine-agnostic (WeasyPrint or the ReportLab
fallback, whichever the environment provides).
"""

from __future__ import annotations

import pytest
from ctk import Artifact, verify

from apx_agent._tool_config import _registry


@pytest.mark.unit
async def test_precall_render_pdf_produces_valid_pdf(tmp_path) -> None:
    # "render_pdf" resolves from the tool registry (usable from [[tool.apx.tools]]).
    registry = _registry()
    assert "render_pdf" in registry, sorted(registry)

    tool = registry["render_pdf"]()
    assert tool.__name__ == "render_pdf"

    markdown = "# Pre-Call Brief: Acme Corp\n\n## Open Orders\n\nORDER-1234 shipped.\n"
    out = tmp_path / "brief.pdf"
    result = await tool(markdown, filename=str(out))

    assert result["bytes"] > 1000, result
    # Reality, not existence: real PDF bytes, > 1KB, starts with the %PDF magic.
    verify(Artifact(str(out), min_bytes=1000, must_contain="%PDF"))
