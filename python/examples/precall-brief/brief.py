"""Headless pre-call brief assembler — the PreCallBriefAgent's deterministic core.

Config-driven (``precall.toml``): for each ``[[precall.section]]`` it
reads that section's frozen view for one company via an injected ``run_view``
callable and renders a markdown brief. No LLM and no live workspace, so the
output is deterministic and the AC-3 gate can assert exact section titles and
seeded per-company values.

``run_view(view, company) -> list[row-dict]`` is the one seam: the gate injects
synthetic rows; deployment injects ``sql_tool`` (``SELECT * FROM <view> WHERE
company = :company``). Swapping synthetic->real is invisible here (G4).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Callable

# precall-brief/brief.py -> precall-brief/precall.toml
_CONFIG = Path(__file__).resolve().parent / "precall.toml"

RunView = Callable[[str, str], list[dict[str, Any]]]


def load_sections(config_path: Path = _CONFIG) -> list[tuple[str, str]]:
    """Return ``[(title, view), ...]`` in render order from the TOML config."""
    cfg = tomllib.loads(config_path.read_text())
    return [(s["title"], s["view"]) for s in cfg["precall"]["section"]]


def _render_section(title: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"## {title}\n\n_No records._\n"
    cols = list(rows[0])
    header = "| " + " | ".join(cols) + " |"
    divider = "| " + " | ".join("---" for _ in cols) + " |"
    body = [
        "| " + " | ".join(str(row[c]) for c in cols) + " |" for row in rows
    ]
    return "\n".join([f"## {title}", "", header, divider, *body, ""])


def build_brief(
    company: str,
    run_view: RunView,
    config_path: Path = _CONFIG,
) -> str:
    """Render the full markdown brief for ``company``.

    Every configured section title is emitted (even when empty) so the brief is
    a complete, predictable 7-section document.
    """
    parts = [f"# Pre-Call Brief: {company}", ""]
    for title, view in load_sections(config_path):
        parts.append(_render_section(title, run_view(view, company)))
    return "\n".join(parts)
