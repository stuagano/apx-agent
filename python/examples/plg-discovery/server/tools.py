"""Python tools compiled into the native AppKit discovery agent."""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

# ---------------------------------------------------------------- web research
_MAX_FETCH_CHARS = 8000


def _embedded_hosts(raw: str, page_url: str) -> list[str]:
    """Distinct third-party hostnames a page embeds — from src/href/action
    attributes and raw URLs (incl. inside <script>). These reveal the SaaS a page
    uses (donation platform, CRM widget, analytics) that the visible text never
    shows. The page's own host is excluded so only third parties surface."""
    urls = re.findall(
        r"""(?:src|href|action)\s*=\s*["']([^"']+)["']""", raw, flags=re.I
    )
    urls += re.findall(
        r"""https?://[^\s"'<>()]+""", raw
    )  # URLs inside scripts/JSON too

    def _host(u: str) -> str:
        # urlparse raises ValueError on malformed tokens (e.g. "https://[",
        # scraped from arbitrary page content). This tool must never raise — a
        # raising tool aborts the whole agent turn — so degrade to "" and skip.
        try:
            hostname = urlparse(u).hostname
        except ValueError:
            return ""
        if hostname is None:
            return ""
        h = hostname.lower()
        return h[4:] if h.startswith("www.") else h

    page_host = _host(page_url)
    hosts: list[str] = []
    seen: set[str] = set()
    for u in urls:
        h = _host(u)
        if not h or h == page_host or h in seen:
            continue
        seen.add(h)
        hosts.append(h)
    return hosts


def fetch_web_page(url: str) -> str:
    """Fetch a public web page: its visible text PLUS the third-party services it
    embeds. Use this to research an organization — its website, its donate page
    (to identify the giving platform), and public records like ProPublica.

    The result ends with an "[embedded third-party services & links]" list of the
    external hosts the page references (e.g. an eTapestry/Blackbaud iframe on a
    donate page), which is how you identify a SaaS provider — the visible text
    alone won't show it.

    On ANY fetch failure (404, redirect-to-404, timeout, DNS/connection error,
    …) this returns a short error string rather than raising: a tool that raises
    aborts the entire agent turn, so the model must instead see the failure as a
    result and recover (try another URL, or just ask the user)."""
    req = urllib.request.Request(url, headers={"User-Agent": "plg-discovery/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted dev use)
            raw = resp.read(_MAX_FETCH_CHARS * 4).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — must return, not raise, to keep the turn alive
        return (
            f"[web_research error] could not fetch {url!r}: {type(exc).__name__}: {exc}"
        )
    # Pull embedded resource hosts BEFORE stripping tags (script/style/tag removal
    # below would otherwise discard them).
    hosts = _embedded_hosts(raw, url)
    services = ", ".join(hosts[:40]) if hosts else "(none detected)"
    # crude tag strip — enough to give the LLM readable context
    text = re.sub(
        r"<script.*?</script>|<style.*?</style>",
        " ",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()[:_MAX_FETCH_CHARS]
    return f"{text}\n\n[embedded third-party services & links]\n{services}"


_skills: dict[str, dict] = {}


def reset_runtime() -> None:
    _skills.clear()


# ---------------------------------------------------------------- skills
def _make_skill_callable(name: str, description: str, content: str):
    def _skill() -> str:
        return content

    _skill.__name__ = name
    _skill.__doc__ = description or f"Return the '{name}' skill guidance."
    return _skill


_SKILLS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "skills"


class _SkillFile(NamedTuple):
    description: str
    content: str


def _parse_skill_file(text: str) -> _SkillFile:
    """Parse a shipped skill file. A leading ``description: ...`` line (optionally
    followed by a blank separator) becomes the description; the rest is the content."""
    lines = text.splitlines()
    description = ""
    start = 0
    if lines and lines[0].lower().startswith("description:"):
        description = lines[0].split(":", 1)[1].strip()
        start = 1
        if start < len(lines) and not lines[start].strip():
            start += 1
    return _SkillFile(description, "\n".join(lines[start:]).strip())


def load_default_skills() -> None:
    """Register the shipped markdown skills before the APX manifest is compiled."""
    if not _SKILLS_DIR.is_dir():
        return
    for path in sorted(_SKILLS_DIR.glob("*.md")):
        skill_file = _parse_skill_file(path.read_text())
        set_skill(path.stem, skill_file.description, skill_file.content)


def set_skill(name: str, description: str, content: str) -> None:
    _skills[name] = {"description": description, "content": content}


def remove_skill(name: str) -> None:
    _skills.pop(name, None)


def list_skills() -> list[dict]:
    return [{"name": n, "description": s["description"]} for n, s in _skills.items()]


def active_tools() -> list:
    """Return the fixed Python tool surface compiled into the AppKit manifest."""
    fns: list = [fetch_web_page]
    for name, s in _skills.items():
        fns.append(_make_skill_callable(name, s["description"], s["content"]))
    return fns
