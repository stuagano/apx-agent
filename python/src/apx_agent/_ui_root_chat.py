"""End-user chat at ``/`` — the public face of a deployed agent.

Distinct from the dev console under ``/_apx/*`` (which is intentionally off in
Apps deployments). This surface ships in *every* runtime and carries no dev
write-guard. Markdown libs are inlined from ``_static/vendor`` so the page is
self-contained (no ``/vendor`` asset route, no CDN — private-link-safe).

It talks to ``POST /responses`` (the MLflow ResponsesAgent contract:
``{input:[...]}`` → ``{output:[...]}``). That endpoint is served identically by
both serve paths — ``create_app`` (local ``apx-agent run``) and the AgentServer
used on Apps deploys — so one page works everywhere. ``/invocations`` is NOT
used here: it means ChatAgent ``{messages}`` under ``create_app`` but
ResponsesAgent ``{input}`` under Apps, so it can't be a single contract.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

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
<title>Agent</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.5 system-ui, -apple-system, sans-serif;
         display: flex; flex-direction: column; height: 100vh; }
  header { padding: 12px 16px; border-bottom: 1px solid #8884; font-weight: 600; }
  #log { flex: 1; overflow-y: auto; padding: 16px; display: flex;
         flex-direction: column; gap: 12px; }
  .msg { max-width: 760px; width: fit-content; padding: 10px 14px;
         border-radius: 12px; white-space: normal; word-wrap: break-word; }
  .user { align-self: flex-end; background: #2563eb; color: #fff; }
  .assistant { align-self: flex-start; background: #8882; }
  .assistant pre { overflow-x: auto; background: #0002; padding: 8px;
                   border-radius: 6px; }
  .assistant p:first-child { margin-top: 0; }
  .assistant p:last-child { margin-bottom: 0; }
  .err { align-self: center; color: #c00; font-size: 13px; }
  form { display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid #8884; }
  textarea { flex: 1; resize: none; padding: 10px; border-radius: 8px;
             border: 1px solid #8886; font: inherit; background: transparent;
             color: inherit; max-height: 160px; }
  button { padding: 0 18px; border: 0; border-radius: 8px; background: #2563eb;
           color: #fff; font: inherit; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  .dots::after { content: '...'; animation: blink 1.2s steps(4) infinite; }
  @keyframes blink { to { clip-path: inset(0 100% 0 0); } }
</style>
__VENDOR__
</head>
<body>
<header>Agent</header>
<div id="log"></div>
<form id="f">
  <textarea id="in" rows="1" placeholder="Message the agent..." autofocus></textarea>
  <button id="send" type="submit">Send</button>
</form>
<script>
  const log = document.getElementById('log');
  const input = document.getElementById('in');
  const sendBtn = document.getElementById('send');
  const form = document.getElementById('f');
  const history = [];  // [{role, content}] sent as the /responses `input`

  const hasMd = typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined';
  function renderMarkdown(el, text) {
    if (hasMd) el.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
    else el.textContent = text || '';
  }

  function bubble(role, text) {
    const el = document.createElement('div');
    el.className = 'msg ' + role;
    if (role === 'assistant') renderMarkdown(el, text);
    else el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function errLine(text) {
    const el = document.createElement('div');
    el.className = 'err';
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  async function send(text) {
    history.push({ role: 'user', content: text });
    bubble('user', text);
    sendBtn.disabled = true;
    const pending = bubble('assistant', '');
    pending.classList.add('dots');
    try {
      const resp = await fetch('/responses', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: history }),
      });
      if (!resp.ok) throw new Error(resp.status + ' ' + (await resp.text()).slice(0, 300));
      const data = await resp.json();
      // ResponsesAgent output: items of {type}. Collect assistant text from
      // `message` items; ignore function_call / function_call_output (tools).
      const out = Array.isArray(data.output) ? data.output : [];
      let reply = '';
      for (const item of out) {
        if (item.type === 'message' && Array.isArray(item.content)) {
          for (const p of item.content) {
            if (p.type === 'output_text' && p.text) reply += p.text;
          }
        }
      }
      pending.classList.remove('dots');
      renderMarkdown(pending, reply || '(empty response)');
      history.push({ role: 'assistant', content: reply });
    } catch (e) {
      pending.remove();
      errLine('Error: ' + e.message);
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.style.height = 'auto';
    send(text);
  });

  // Enter sends, Shift+Enter newlines; textarea grows with content.
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = input.scrollHeight + 'px';
  });
</script>
</body>
</html>
"""


def render_root_chat() -> str:
    """The full self-contained chat page."""
    return _PAGE.replace("__VENDOR__", _inline_vendor())


def build_root_chat_router() -> APIRouter:
    """Router serving the end-user chat at ``GET /``.

    Mounted in every runtime (including Apps) — unlike the dev UI.
    """
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def root_chat() -> HTMLResponse:
        return HTMLResponse(render_root_chat())

    return router
