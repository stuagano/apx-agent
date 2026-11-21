"""Du Bois theme tokens + global injection for the /_apx/* dev UI."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from apx_agent._ui_nav import APX_NAV_PAGES
from apx_agent._ui_theme import (
    APX_COMPONENTS_CSS,
    APX_THEME_CSS,
    apx_theme_style,
)


def test_theme_defines_dubois_tokens():
    # Du Bois values (@databricks/design-system dubois-colors.less), dark.
    for var, value in [
        ("--apx-bg", "#11171C"),
        ("--apx-panel", "#1F272D"),
        ("--apx-border", "#445461"),
        ("--apx-text", "#E8ECF0"),
        ("--apx-accent", "#2272B4"),
    ]:
        assert var in APX_THEME_CSS, f"{var} missing from theme"
        assert f"{var}:{value}" in APX_THEME_CSS, f"{var} should use Du Bois value {value}"
    assert "color-scheme:dark" in APX_THEME_CSS


def test_theme_style_is_one_style_block():
    s = apx_theme_style()
    assert s.startswith("<style>") and s.endswith("</style>")
    assert "apx-dubois-theme" in s


def test_shared_components_defined_with_tokens():
    for cls in [".apx-card", ".apx-badge", ".apx-btn", ".apx-empty", ".apx-pre"]:
        assert cls in APX_COMPONENTS_CSS, f"{cls} must be a shared primitive"
    hexes = re.findall(r"#[0-9a-fA-F]{3,6}", APX_COMPONENTS_CSS)
    assert not hexes, f"shared components must use var(--apx-*), found: {hexes}"


async def _get(app, path):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        return await ac.get(path, follow_redirects=True)


# The theme is injected into each server-rendered page's <head> (the true
# universal choke point; the deploy overlay is dropped on embed/some pages).
# `knowledge` always renders its <head> server-side, so it's the mechanism proof.
# Discover is now a React SPA, like topology, so it is out of scope here.
# Whole-surface coverage across all nav slugs is asserted in test_dubois_sweep
# once every module has been migrated (topology is a React SPA — out of scope).
@pytest.mark.asyncio
async def test_knowledge_page_carries_theme_once(dev_ui_app):
    r = await _get(dev_ui_app, "/_apx/knowledge")
    assert r.status_code == 200
    assert r.text.count("apx-dubois-theme") == 1, (
        "the Du Bois theme should be injected exactly once into the knowledge "
        "page <head> via apx_theme_style()"
    )


_SRC = Path(__file__).resolve().parents[1] / "src" / "apx_agent"
# 3-digit matches must look like CSS: "(#629)" / "#611)" are issue references.
_HEX = re.compile(r"(?<![&\w])#[0-9a-fA-F]{6}\b|(?<![&\w(])#[0-9a-fA-F]{3}(?=[;\s'\"},])")
_PROP = re.compile(r"(--[\w-]+)\s*:\s*([^;}{]+)")
_VAR = re.compile(r"var\(\s*(--[\w-]+)\s*\)")
_HEX_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_CARD = re.compile(r"\.([\w-]*card)\{([^}]*)\}")
# `border-radius` / `background-image` do not match: the optional suffix is only `-color`.
_FILL = re.compile(r"(?:^|[{;\s])background(?:-color)?\s*:\s*([^;]+)")
_STROKE = re.compile(r"(?:^|[{;\s])border(?:-color)?\s*:\s*([^;]+)")


def _swept_ui_modules() -> list[str]:
    """UI modules the hex guard protects, taken from the tree.

    `_ui_theme.py` is the Du Bois token source, so its hex literals stay.
    `_dev.py` hosts the trace cards and is not named `_ui_*`.
    """
    pages = sorted(p.name for p in _SRC.glob("_ui_*.py") if p.name != "_ui_theme.py")
    return pages + ["_dev.py"]


_SWEPT = _swept_ui_modules()


@pytest.mark.parametrize("name", _SWEPT)
def test_pages_use_theme_tokens_not_hex(name: str) -> None:
    hexes = _HEX.findall((_SRC / name).read_text(encoding="utf-8"))
    assert hexes == [], f"{name} hard-codes colors {sorted(set(hexes))[:12]}; use var(--apx-*)"


def test_hex_guard_covers_files_the_old_list_missed() -> None:
    # The hand-kept list of eight stopped at knowledge/edit/nav/tools/hub/table/
    # grounding/dev. Chat (and any later `_ui_*.py`) has to come from the tree.
    on_disk = {p.name for p in _SRC.glob("_ui_*.py") if p.name != "_ui_theme.py"}
    assert "_ui_chat.py" in on_disk
    assert on_disk <= set(_SWEPT)
    assert "_dev.py" in _SWEPT
    assert len(_SWEPT) > 8


def _token_values(*sheets: str) -> dict[str, str]:
    raw: dict[str, str] = {}
    for sheet in sheets:
        for name, value in _PROP.findall(sheet):
            raw[name] = value.strip()
    return raw


def _paint(value: str, raw: dict[str, str]) -> str:
    seen: set[str] = set()
    current = value
    for _ in range(8):
        ref = _VAR.search(current)
        if ref is None:
            break
        name = ref.group(1)
        if name in seen or name not in raw:
            break
        seen.add(name)
        current = raw[name]
    found = _HEX_LITERAL.findall(current)
    if not found:
        return current.strip().lower()
    return found[-1].lower()


def _linear(channel: int) -> float:
    raw = channel / 255
    if raw <= 0.04045:
        return raw / 12.92
    return ((raw + 0.055) / 1.055) ** 2.4


def _contrast(a: str, b: str) -> float:
    def lum(color: str) -> float:
        h = color[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        r, g, bl = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(bl)

    lighter = max(lum(a), lum(b))
    darker = min(lum(a), lum(b))
    return (lighter + 0.05) / (darker + 0.05)


def test_card_border_does_not_collapse_into_background() -> None:
    """A card whose border colour equals its fill has no visible edge.

    `--apx-border` on `--apx-panel-2` is only ~1.28:1 and still disappears.
    The shared `.apx-card` pairing is the floor.
    """
    dev = (_SRC / "_dev.py").read_text(encoding="utf-8")
    raw = _token_values(APX_THEME_CSS, dev)
    ratios: dict[str, float] = {}
    for cls, body in _CARD.findall(dev + APX_COMPONENTS_CSS):
        fill = _FILL.search(body)
        stroke = _STROKE.search(body)
        if fill is None or stroke is None:
            continue
        bg = _paint(fill.group(1), raw)
        border = _paint(stroke.group(1), raw)
        assert bg.startswith("#") and border.startswith("#"), (
            f".{cls} colours did not resolve ({bg!r} / {border!r})"
        )
        ratios[cls] = _contrast(bg, border)
        assert ratios[cls] > 1, f".{cls} border {border} collapses into background {bg}"
    assert {"tool-fails-card", "trace-diff-card", "span-card", "apx-card"} <= set(ratios)
    floor = ratios["apx-card"]
    for cls, ratio in ratios.items():
        assert ratio >= floor, (
            f".{cls} border/fill contrast {ratio:.2f} is below .apx-card ({floor:.2f})"
        )


def test_no_page_forces_its_own_color_scheme() -> None:
    # The theme owns color-scheme (dark Du Bois); a page forcing one would fight it.
    for path in sorted(p for p in _SRC.glob("_ui_*.py") if p.name != "_ui_theme.py") + [_SRC / "_dev.py"]:
        assert "color-scheme:dark" not in path.read_text(encoding="utf-8").replace(" ", ""), path.name


def test_feedback_save_hover_keeps_label_readable() -> None:
    css = (_SRC / "_dev.py").read_text(encoding="utf-8")
    hover = re.search(r"\.fb-save-btn:hover\{\{([^}]*)\}\}", css)
    assert hover and "color:var(--apx-on-accent)" in hover.group(1), "hover fills --apx-ok; label must contrast"
