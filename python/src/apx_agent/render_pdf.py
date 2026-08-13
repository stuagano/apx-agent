"""render_pdf — turn a markdown brief into a PDF file (serverless-safe).

The one net-new capability for the pre-call brief agent: everything else is
assembly on existing primitives. Renders the brief markdown to a PDF via
WeasyPrint when its native libs are present (best fidelity), else falls back to
ReportLab (pure-Python, always available on serverless App compute). The tool
takes no governed workspace resource — it is pure compute — so ``build_tool``
attaches no ``ResourceSpec``.

Annotations are intentionally NOT deferred (no ``from __future__ import
annotations``) so the runtime resolves the tool's parameter types eagerly, the
same convention every other factory module follows.
"""

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _markdown_to_pdf_bytes(markdown: str) -> bytes:
    """Render markdown -> PDF bytes. WeasyPrint if available, else ReportLab."""
    try:
        import weasyprint  # type: ignore[import-untyped]

        html = _markdown_to_html(markdown)
        return weasyprint.HTML(string=html).write_pdf()
    except Exception as e:  # ImportError, or missing native libs (OSError) on serverless
        logger.info("WeasyPrint unavailable (%s) — using ReportLab fallback.", e)
        return _reportlab_pdf_bytes(markdown)


def _markdown_to_html(markdown: str) -> str:
    """Minimal markdown->HTML — headings and paragraphs are enough for a brief."""
    parts: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            level = len(m.group(1))
            parts.append(f"<h{level}>{m.group(2)}</h{level}>")
        else:
            parts.append(f"<p>{stripped}</p>")
    return "<html><body>" + "".join(parts) + "</body></html>"


def _reportlab_pdf_bytes(markdown: str) -> bytes:
    """Pure-Python PDF via ReportLab — no native deps, serverless-safe."""
    import io

    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf)
    flow: list[Any] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            flow.append(Spacer(1, 6))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", stripped)
        if m:
            level = min(len(m.group(1)), 3)  # ReportLab ships Heading1..3
            flow.append(Paragraph(m.group(2), styles[f"Heading{level}"]))
        else:
            flow.append(Paragraph(stripped, styles["BodyText"]))
    doc.build(flow or [Paragraph("(empty brief)", styles["BodyText"])])
    return buf.getvalue()


def render_pdf(*, name: str = "render_pdf", description: str | None = None) -> Any:
    """Return a tool that renders a markdown brief to a PDF file.

    The inner callable takes the brief markdown and an optional output filename,
    writes the PDF, and returns its path and byte size. WeasyPrint is used when
    available; otherwise ReportLab. No workspace client / governed resource is
    needed (pure compute).

    Args:
        name: LLM-facing tool name. Defaults to ``"render_pdf"``.
        description: LLM-facing description. A default is generated when omitted.
    """
    from ._tool_factory import build_tool

    _desc = description or (
        "Render a markdown brief to a PDF file and return its path and size. "
        "Pass the full brief markdown; optionally a filename."
    )

    async def _render(markdown: str, filename: str = "brief.pdf") -> dict[str, Any]:
        """Placeholder doc — overwritten by build_tool."""
        pdf_bytes = _markdown_to_pdf_bytes(markdown)
        out = Path(filename)
        out.write_bytes(pdf_bytes)
        return {"path": str(out.resolve()), "bytes": len(pdf_bytes)}

    return build_tool(_render, name=name, description=_desc, resources=())
