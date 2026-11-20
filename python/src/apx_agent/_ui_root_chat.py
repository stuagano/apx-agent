"""End-user chat at ``/`` — the public face of a deployed agent.

Distinct from the dev console under ``/_apx/*`` (which is intentionally off in
Apps deployments). This surface ships in *every* runtime and carries no dev
write-guard. Markdown libs are inlined from ``_static/vendor`` (no CDN —
private-link-safe). The page's own script is a same-origin asset this router
serves at ``/assets/chat/``, so it does not depend on the dev UI.

The host selects native ``/api/invocations`` or declared legacy ``/invocations``
transport. Native reconnects read persisted state and events without resubmission.
"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ._ui_theme import apx_theme_style

_VENDOR = Path(__file__).parent / "_static" / "vendor"


def _inline_vendor() -> str:
    """Read marked + purify and wrap them in a single inline <script>."""
    parts = []
    for name in ("marked.min.js", "purify.min.js"):
        f = _VENDOR / name
        # ponytail: if a vendor file is missing the page still loads; markdown
        # just won't render (renderMarkdown falls back to plain text).
        if f.is_file():
            parts.append(f.read_text(encoding="utf-8"))
    return "<script>\n" + "\n".join(parts) + "\n</script>"


# Plain string (NOT a format string) — JS braces are literal. The only dynamic
# piece is the inlined vendor <script>, spliced in by build_root_chat_router.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
__THEME__
<style>
  /* Palette uses the design tokens from _ui_theme. */
  * { box-sizing: border-box; }
  body { margin: 0; font-family: var(--apx-font);
         font-size: 14px; line-height: 1.5; display: flex; flex-direction: column;
         height: 100vh; background: var(--apx-bg); color: var(--apx-text); }
  header { padding: 12px 16px; border-bottom: 1px solid var(--apx-border);
           background: var(--apx-panel); flex-shrink: 0;
           display: flex; align-items: baseline; gap: 10px; }
  header .name { font-weight: 600; }
  header .desc { font-size: 13px; color: var(--apx-muted); }
  #log { flex: 1; overflow-y: auto; padding: 16px; display: flex;
         flex-direction: column; gap: 12px; }
  .msg { max-width: 760px; width: fit-content; padding: 10px 14px;
         border-radius: 12px; white-space: normal; word-wrap: break-word; }
  .user { align-self: flex-end; background: var(--apx-accent); color: var(--apx-on-accent);
          border: 1px solid var(--apx-accent-hover); }
  .assistant { align-self: flex-start; background: var(--apx-panel);
               border: 1px solid var(--apx-border); }
  .assistant a { color: var(--apx-accent); }
  .assistant pre { overflow-x: auto; background: var(--apx-bg); padding: 8px;
                   border-radius: 6px; border: 1px solid var(--apx-border); }
  .assistant table { display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; margin: 8px 0; font-size: 13px; }
  .assistant th, .assistant td { border: 1px solid var(--apx-border); padding: 4px 8px; text-align: left; }
  .assistant th { font-weight: 600; background: var(--apx-bg); }
  .assistant p:first-child { margin-top: 0; }
  .assistant p:last-child { margin-bottom: 0; }
  .err { align-self: center; color: var(--apx-err); font-size: 13px; }
  form { display: flex; gap: 8px; padding: 12px 16px;
         border-top: 1px solid var(--apx-border); background: var(--apx-panel); }
  textarea { flex: 1; resize: none; padding: 10px; border-radius: 8px;
             border: 1px solid var(--apx-border); font: inherit;
             background: var(--apx-bg); color: var(--apx-text); max-height: 160px; }
  textarea:focus { outline: none; border-color: var(--apx-accent); }
  button { padding: 0 18px; border: 1px solid var(--apx-border); border-radius: 8px;
           background: transparent; color: var(--apx-accent); font: inherit; cursor: pointer; }
  button:hover:not(:disabled) { border-color: var(--apx-accent); background: var(--apx-accent); color: var(--apx-on-accent); }
  button:disabled { opacity: .5; cursor: default; }
  .dots::after { content: '...'; animation: blink 1.2s steps(4) infinite; }
  @keyframes blink { to { clip-path: inset(0 100% 0 0); } }
</style>
__VENDOR__
</head>
<body>
<header>__HEADER__</header>
<div id="log"></div>
<form id="f">
  <textarea id="in" rows="1" placeholder="Message the agent..." autofocus></textarea>
  <button id="send" type="submit">Send</button>
</form>
<script>window.__APX_NATIVE__ = __NATIVE__;</script>
<script src="/assets/chat/root-chat.js"></script>
__NATIVE_SCRIPT__
</body>
</html>
"""


def render_root_chat(name: str = "Agent", description: str | None = None, *, native: bool = False) -> str:
    """The full self-contained chat page, titled for this agent.

    :param name: Agent name shown in the title bar and tab. Escaped.
    :param description: Optional one-line subtitle next to the name. Escaped.
    """
    safe_name = html.escape(name) or "Agent"
    title = safe_name
    header = f'<span class="name">{safe_name}</span>'
    if description:
        safe_desc = html.escape(description)
        header += f'<span class="desc">{safe_desc}</span>'
    theme = apx_theme_style()
    from ._ui_native_chat import native_chat_script

    return (
        _PAGE.replace("__VENDOR__", _inline_vendor())
        .replace("__THEME__", theme)
        .replace("__TITLE__", title)
        .replace("__HEADER__", header)
        .replace("__NATIVE__", str(native).lower())
        .replace("__NATIVE_SCRIPT__", native_chat_script(root=True) if native else "")
    )


_CHAT_ASSETS = ("root-chat.js", "native-chat.js")


def build_root_chat_router() -> APIRouter:
    """Router serving the end-user chat at ``GET /``.

    Mounted in every runtime (including Apps) — unlike the dev UI. The page is
    titled from the served agent's config (``app.state.agent_context``), read at
    request time so it reflects whatever agent this app is serving.
    """
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    router = APIRouter()

    @router.get("/assets/chat/{filename}", include_in_schema=False)
    async def chat_asset(filename: str) -> FileResponse:
        if filename not in _CHAT_ASSETS:
            raise HTTPException(status_code=404)
        target = (_VENDOR / filename).resolve()
        if _VENDOR.resolve() not in target.parents or not target.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(target, media_type="application/javascript")

    @router.get("/", include_in_schema=False)
    async def root_chat(request: Request) -> HTMLResponse:
        ctx = getattr(request.app.state, "agent_context", None)
        cfg = getattr(ctx, "config", None)
        name = getattr(cfg, "name", None) or "Agent"
        description = getattr(cfg, "description", None)
        return HTMLResponse(render_root_chat(
            name, description, native=getattr(request.app.state, "_apx_native_chat", False),
        ))

    return router
