"""Dev UI — /_apx/agent chat interface, OpenAPI spec builder, and /_apx/tools inspector."""

from __future__ import annotations

from typing import Any

from ._models import AgentContext
from ._ui_edit import _find_agent_router_path
from ._ui_nav import _apx_nav_css, _apx_nav_html, _deploy_overlay_html

def _render_agent_ui(ctx: AgentContext | None) -> str:
    """Return a self-contained HTML page for interactively testing the agent."""
    import json as _json

    agent_name = ctx.config.name if ctx else "Agent"
    agent_desc = ctx.config.description if ctx else ""
    tools_json = (
        _json.dumps([{
            "name": t.name, "description": t.description,
            "schema": t.input_schema or {"type": "object", "properties": {}},
            "remote": bool(t.sub_agent_url),
        } for t in ctx.tools if t.name != "create_tool"])
        if ctx else "[]"
    )
    not_configured = ctx is None
    setup_banner = """
<div id="setup-banner">
  <strong>⚠ Agent not configured</strong><br>
  Add <code>[tool.apx.agent]</code> to <code>pyproject.toml</code> and create
  <code>src/{app}/backend/agent_router.py</code> with an <code>Agent(tools=[...])</code> call,
  then restart the dev server.
</div>""" if not_configured else ""
    # First-run wizard nudge: show banner if no catalog/warehouse configured
    if not not_configured and ctx:
        _env_catalog = os.environ.get("DEMO_CATALOG") or os.environ.get("CATALOG", "")
        _env_wh = os.environ.get("WAREHOUSE_ID", "")
        if not _env_catalog or not _env_wh:
            setup_banner = (
                '<div id="setup-banner" style="background:#1a1200;border-color:#5a3a00;color:#ffb84d">'
                '<strong>👋 First time here?</strong> '
                '<a href="/_apx/setup" style="color:#ffd080;text-decoration:underline">Open Setup</a> '
                'to connect your data and generate tools automatically.'
                '</div>'
            )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{agent_name} — APX Dev</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0a0a0a; color: #e8e8e8; height: 100vh; display: flex; flex-direction: column; }}

  /* --- Header --- */
  header {{ padding: 14px 24px; background: #111; border-bottom: 1px solid #222;
            display: flex; align-items: center; gap: 14px; flex-shrink: 0; }}
  .badge {{ background: #1e3a5f; color: #60b0ff; font-size: 11px; font-weight: 600;
            padding: 3px 10px; border-radius: 4px; letter-spacing: .5px; text-transform: uppercase; }}
  header h1 {{ font-size: 17px; font-weight: 600; color: #fff; }}
  header .desc {{ font-size: 13px; color: #555; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  nav {{ display: flex; gap: 4px; }}
  nav a {{ font-size: 12px; color: #777; text-decoration: none; padding: 5px 12px;
           border-radius: 6px; border: 1px solid transparent; }}
  nav a:hover {{ color: #ccc; border-color: #333; }}
  nav a.active {{ color: #60b0ff; background: #0d1f38; border-color: #1e3a5f; }}

  /* --- Main layout: chat left, tools right --- */
  .main {{ flex: 1; display: flex; overflow: hidden; }}

  /* --- Chat panel (left/center) --- */
  .chat-panel {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
  #chat {{ flex: 1; overflow-y: auto; padding: 28px 32px; display: flex; flex-direction: column; gap: 16px; }}
  .msg {{ max-width: 720px; line-height: 1.6; font-size: 15px; }}
  .msg.user {{ align-self: flex-end; background: #1a3a5c; color: #cce4ff;
               padding: 12px 18px; border-radius: 16px 16px 4px 16px; }}
  .msg.assistant {{ align-self: flex-start; color: #ddd; white-space: pre-wrap; }}
  .msg.assistant.streaming::after {{ content: "▋"; animation: blink .7s step-end infinite; }}
  .msg.system {{ align-self: center; font-size: 13px; color: #444; font-style: italic; padding: 20px 0; }}
  @keyframes blink {{ 50% {{ opacity: 0; }} }}

  /* Inline tool call pills */
  .tool-pills {{ align-self: flex-start; display: flex; flex-wrap: wrap; gap: 8px; margin: 2px 0 4px; }}
  .tool-pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px;
                 border-radius: 8px; font-size: 13px; font-family: monospace; cursor: pointer;
                 border: 1px solid #222; transition: all .15s; }}
  .tool-pill:hover {{ border-color: #555; transform: translateY(-1px); }}
  .tool-pill.call {{ background: #0d1a2e; color: #60b0ff; border-color: #1a2a4a; }}
  .tool-pill.result {{ background: #0a1a0a; color: #4ade80; border-color: #1a3a1a; }}
  .tool-pill.error {{ background: #1a0a0a; color: #f87171; border-color: #3a1a1a; }}
  .tool-pill .icon {{ font-size: 12px; }}
  .tool-pill .ms {{ font-size: 11px; color: #555; margin-left: 4px; }}

  /* Input area */
  .input-bar {{ display: flex; gap: 10px; padding: 16px 24px; background: #111;
                 border-top: 1px solid #222; flex-shrink: 0; }}
  .input-bar textarea {{ flex: 1; background: #161616; border: 1px solid #2a2a2a; color: #e8e8e8;
                          border-radius: 10px; padding: 12px 16px; font-size: 15px; resize: none;
                          font-family: inherit; line-height: 1.5; outline: none; max-height: 160px; }}
  .input-bar textarea:focus {{ border-color: #3a7bd5; }}
  .input-bar button {{ background: #2563eb; color: #fff; border: none; border-radius: 10px;
                        padding: 12px 20px; font-size: 14px; cursor: pointer; align-self: flex-end;
                        white-space: nowrap; font-weight: 500; }}
  .input-bar button:hover {{ background: #1d4ed8; }}
  .input-bar button:disabled {{ background: #1a3060; color: #555; cursor: not-allowed; }}

  /* --- Right panel: tools & events --- */
  .resize-handle {{ width: 5px; cursor: col-resize; background: transparent; flex-shrink: 0; }}
  .resize-handle:hover {{ background: #2563eb; }}
  .right-panel {{ width: 420px; min-width: 280px; max-width: 700px; background: #0d0d0d;
                   border-left: 1px solid #1a1a1a; display: flex; flex-direction: column; flex-shrink: 0; }}
  .panel-tabs {{ display: flex; border-bottom: 1px solid #1a1a1a; flex-shrink: 0; }}
  .panel-tabs button {{ flex: 1; background: none; border: none; color: #555; font-size: 13px; font-weight: 500;
                          padding: 12px 0; cursor: pointer; border-bottom: 2px solid transparent; transition: all .15s; }}
  .panel-tabs button:hover {{ color: #aaa; }}
  .panel-tabs button.active {{ color: #60b0ff; border-bottom-color: #60b0ff; }}
  .panel-content {{ flex: 1; overflow-y: auto; }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}

  /* Events list */
  .event {{ display: flex; align-items: flex-start; gap: 10px; padding: 10px 16px; border-bottom: 1px solid #111;
            cursor: pointer; font-size: 13px; line-height: 1.4; transition: background .1s; }}
  .event:hover {{ background: #151515; }}
  .event.selected {{ background: #0d1f38; }}
  .event-num {{ color: #444; font-size: 12px; font-family: monospace; min-width: 26px; text-align: right; flex-shrink: 0; }}
  .event-icon {{ flex-shrink: 0; font-size: 14px; }}
  .event-body {{ flex: 1; min-width: 0; }}
  .event-title {{ color: #bbb; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .event-sub {{ color: #555; font-size: 12px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .event.tool-call .event-title {{ color: #60b0ff; }}
  .event.tool-result .event-title {{ color: #4ade80; }}
  .event.tool-error .event-title {{ color: #f87171; }}

  /* Tool test panel */
  .tool-card {{ border-bottom: 1px solid #1a1a1a; }}
  .tool-card-header {{ padding: 12px 16px; cursor: pointer; display: flex; align-items: center; gap: 10px; }}
  .tool-card-header:hover {{ background: #111; }}
  .tool-card-header .arrow {{ color: #444; font-size: 10px; transition: transform .15s; }}
  .tool-card.open .arrow {{ transform: rotate(90deg); }}
  .tool-card-header .tname {{ color: #60b0ff; font-size: 14px; font-weight: 600; font-family: monospace; }}
  .tool-card-header .tbadge {{ font-size: 10px; color: #555; background: #1a1a1a; padding: 2px 6px;
                                border-radius: 3px; margin-left: auto; }}
  .btn-delete-tool {{ margin-left: auto; background: none; border: none; color: #333;
                      cursor: pointer; font-size: 12px; padding: 2px 6px; border-radius: 4px; line-height: 1; }}
  .btn-delete-tool:hover {{ color: #f87171; background: #1a0a0a; }}
  .tool-card-body {{ display: none; padding: 0 16px 16px; }}
  .tool-card.open .tool-card-body {{ display: block; }}
  .tool-card-body .tdesc {{ font-size: 12px; color: #666; margin-bottom: 12px; line-height: 1.5; }}
  .tool-card-body label {{ display: block; font-size: 12px; color: #888; margin-bottom: 4px; margin-top: 10px; }}
  .tool-card-body label:first-of-type {{ margin-top: 0; }}
  .tool-card-body input {{ width: 100%; background: #161616; border: 1px solid #2a2a2a; color: #ddd;
                            border-radius: 6px; padding: 8px 12px; font-size: 13px; font-family: monospace;
                            outline: none; }}
  .tool-card-body input:focus {{ border-color: #3a7bd5; }}
  .tool-card-body input::placeholder {{ color: #444; }}
  .tool-run {{ margin-top: 12px; display: flex; gap: 8px; align-items: center; }}
  .tool-run button {{ background: #1a3a1a; color: #4ade80; border: 1px solid #2a4a2a; border-radius: 6px;
                       padding: 7px 16px; font-size: 12px; font-weight: 600; cursor: pointer; }}
  .tool-run button:hover {{ background: #2a4a2a; }}
  .tool-run button:disabled {{ opacity: .5; cursor: not-allowed; }}
  .tool-run .run-ms {{ font-size: 11px; color: #555; }}
  .tool-result-box {{ margin-top: 10px; background: #111; border: 1px solid #1a1a1a; border-radius: 6px;
                       padding: 10px 12px; font-family: monospace; font-size: 12px; color: #aaa;
                       white-space: pre-wrap; word-break: break-all; max-height: 240px; overflow-y: auto; line-height: 1.5; }}
  .tool-result-box.err {{ color: #f87171; border-color: #3a1a1a; }}

  /* Detail overlay */
  .detail-panel {{ border-top: 1px solid #1a1a1a; max-height: 40%; overflow-y: auto;
                    background: #080808; flex-shrink: 0; display: none; }}
  .detail-panel.open {{ display: block; }}
  .detail-header {{ padding: 10px 16px; font-size: 12px; color: #666; display: flex; align-items: center;
                     border-bottom: 1px solid #1a1a1a; position: sticky; top: 0; background: #080808; }}
  .detail-header span {{ flex: 1; }}
  .detail-close {{ background: none; border: none; color: #555; cursor: pointer; font-size: 16px; padding: 0 4px; }}
  .detail-close:hover {{ color: #fff; }}
  .detail-body {{ padding: 12px 16px; }}
  .detail-body pre {{ font-family: monospace; font-size: 12px; color: #aaa; white-space: pre-wrap;
                       word-break: break-all; line-height: 1.6; }}
  .detail-body .label {{ font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: .5px; margin: 10px 0 4px; }}
  .detail-body .label:first-child {{ margin-top: 0; }}

  /* MCP bar */
  .mcp-bar {{ padding: 8px 16px; font-size: 12px; color: #555; border-bottom: 1px solid #1a1a1a;
               display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
  .mcp-bar strong {{ color: #4ade80; font-size: 10px; letter-spacing: .5px; text-transform: uppercase; }}
  .mcp-bar code {{ background: #0a150a; color: #7a7; padding: 2px 8px; border-radius: 3px;
                    font-family: monospace; font-size: 11px; }}
  .mcp-bar .cbtn {{ background: none; border: 1px solid #1a2a1a; color: #556; border-radius: 3px;
                     padding: 1px 8px; font-size: 11px; cursor: pointer; }}
  .mcp-bar .cbtn:hover {{ border-color: #4ade80; color: #4ade80; }}

  /* Tooltip */
  .tooltip {{ display: none; position: fixed; background: #1a1a1a; border: 1px solid #333;
              border-radius: 8px; padding: 10px 14px; font-family: monospace; font-size: 12px;
              color: #aaa; max-width: 500px; max-height: 320px; overflow-y: auto;
              white-space: pre-wrap; word-break: break-all; z-index: 100; line-height: 1.5;
              box-shadow: 0 8px 32px rgba(0,0,0,.6); pointer-events: none; }}
  .tooltip.show {{ display: block; }}

  /* Setup banner */
  #setup-banner {{ background: #2a1a00; border-bottom: 1px solid #5a3a00; color: #ffb84d;
                   padding: 12px 24px; font-size: 13px; line-height: 1.6; flex-shrink: 0; }}
  #setup-banner code {{ background: #1a1000; padding: 1px 5px; border-radius: 3px; font-family: monospace; font-size: 12px; }}
  .empty-state {{ padding: 24px; color: #444; font-size: 13px; text-align: center; }}
</style>
</head>
<body>
<header>
  <span class="badge">APX dev</span>
  <h1>{agent_name}</h1>
  <span class="desc">{agent_desc}</span>
  <nav>
    <a href="/_apx/agent" class="active">Chat</a>
    <a href="/_apx/edit">Edit</a>
    <a href="/_apx/setup">Setup</a>
  </nav>
  <button id="btn-deploy">Deploy ▶</button>
</header>
{setup_banner}
<div class="main">
  <!-- Chat (left) -->
  <div class="chat-panel">
    <div id="chat">
      <div class="msg system">Chat with <strong>{agent_name}</strong> — tool changes hot-reload automatically</div>
    </div>
    <form id="form" class="input-bar" autocomplete="off">
      <textarea id="input" rows="1" placeholder="Type a message…" required></textarea>
      <button id="send-btn" type="submit">Send</button>
    </form>
  </div>

  <div class="resize-handle" id="resize-handle"></div>

  <!-- Right panel: Tools + Events -->
  <div class="right-panel" id="right-panel">
    <div class="mcp-bar" id="mcp-bar" style="display:none">
      <strong>MCP</strong>
      <code id="mcp-url"></code>
      <button class="cbtn" onclick="copyMcpUrl()">Copy</button>
      <span id="copy-ok" style="display:none;color:#4ade80">✓</span>
      <span style="color:#333;margin:0 4px">·</span>
      <span style="color:#556;font-size:11px">SSE:</span>
      <button class="cbtn" onclick="copyMcpSseUrl()" title="Copy /mcp/sse (Claude Desktop, Cursor)">/sse</button>
      <span id="copy-sse-ok" style="display:none;color:#4ade80">✓</span>
    </div>
    <div class="panel-tabs">
      <button class="active" onclick="switchTab('tools',this)">Tools</button>
      <button onclick="switchTab('events',this)">Events</button>
      <button onclick="switchTab('eval',this)">Eval</button>
    </div>
    <div class="panel-content">
      <div id="tab-tools" class="tab-panel active"></div>
      <div id="tab-events" class="tab-panel">
        <div id="events-list" class="empty-state">Send a message to see events</div>
      </div>
      <div id="tab-eval" class="tab-panel">
        <div id="eval-toolbar" style="display:flex;gap:6px;align-items:center;padding:10px 12px;border-bottom:1px solid #1a1a1a;flex-shrink:0">
          <button id="eval-run-all" style="background:#1e3a5f;color:#60b0ff;border:1px solid #2a5298;border-radius:5px;padding:5px 12px;font-size:12px;cursor:pointer">▶ Run All</button>
          <button id="eval-reset" style="background:transparent;color:#555;border:1px solid #2a2a2a;border-radius:5px;padding:5px 10px;font-size:12px;cursor:pointer">↺ Reset</button>
          <span id="eval-status" style="font-size:11px;color:#555;margin-left:4px"></span>
        </div>
        <div id="eval-progress" style="height:2px;background:#1e1e1e"><div id="eval-progress-fill" style="height:100%;background:#2563eb;width:0%;transition:width .3s"></div></div>
        <div id="eval-cases" style="overflow-y:auto;flex:1;padding:6px 0">
          <div style="color:#444;font-size:12px;padding:20px 12px">Click Eval tab to load test cases</div>
        </div>
        <div id="eval-add" style="padding:10px 12px;border-top:1px solid #1a1a1a">
          <textarea id="eval-add-q" placeholder="Add a test question…" rows="2" style="width:100%;background:#111;border:1px solid #222;color:#ccc;border-radius:5px;padding:6px 8px;font-size:12px;resize:none;margin-bottom:6px"></textarea>
          <button id="eval-add-btn" style="background:transparent;color:#555;border:1px solid #2a2a2a;border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer">+ Add</button>
        </div>
      </div>
    </div>
    <div class="detail-panel" id="detail-panel">
      <div class="detail-header">
        <span id="detail-title">Event Detail</span>
        <button class="detail-close" onclick="closeDetail()">✕</button>
      </div>
      <div class="detail-body" id="detail-body"></div>
    </div>
  </div>
</div>

<div class="tooltip" id="tooltip"></div>

<script>
const TOOLS = {tools_json};
const chat = document.getElementById('chat');
const form = document.getElementById('form');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const eventsList = document.getElementById('events-list');
const toolsTab = document.getElementById('tab-tools');
const detailPanel = document.getElementById('detail-panel');
const detailTitle = document.getElementById('detail-title');
const detailBody = document.getElementById('detail-body');
const tooltip = document.getElementById('tooltip');

// ── Render tools tab with invoke forms ──
TOOLS.forEach(t => {{
  const props = (t.schema && t.schema.properties) || {{}};
  const required = (t.schema && t.schema.required) || [];
  const card = document.createElement('div');
  card.className = 'tool-card';
  let fields = '';
  for (const [k, v] of Object.entries(props)) {{
    const req = required.includes(k);
    const ph = v.description || v.type || '';
    fields += `<label>${{k}}${{req ? ' <span style="color:#f87171">*</span>' : ''}}</label>`
      + `<input name="${{k}}" type="text" placeholder="${{ph}}" ${{req ? 'required' : ''}} />`;
  }}
  card.innerHTML =
    `<div class="tool-card-header" onclick="this.parentElement.classList.toggle('open')">` +
      `<span class="arrow">▶</span>` +
      `<span class="tname">${{t.name}}</span>` +
      `<span class="tbadge">${{t.remote ? 'remote' : 'local'}}</span>` +
      (!t.remote ? `<button class="btn-delete-tool" onclick="deleteTool(event,'${{t.name}}')" title="Delete tool">✕</button>` : '') +
    `</div>` +
    `<div class="tool-card-body">` +
      `<div class="tdesc">${{t.description.replace(/\\n/g, ' ')}}</div>` +
      (fields || '<div style="color:#444;font-size:12px">No parameters</div>') +
      `<div class="tool-run">` +
        `<button type="button" onclick="runTool(this, '${{t.name}}')">▶ Run</button>` +
        `<span class="run-ms"></span>` +
      `</div>` +
      `<div class="tool-result-box" style="display:none"></div>` +
    `</div>`;
  toolsTab.appendChild(card);
}});
if (!TOOLS.length) toolsTab.innerHTML = '<div class="empty-state">No tools registered</div>';

async function runTool(btn, name) {{
  const card = btn.closest('.tool-card');
  const inputs = card.querySelectorAll('input[name]');
  const args = {{}};
  inputs.forEach(i => {{ if (i.value) args[i.name] = i.value; }});
  const resultBox = card.querySelector('.tool-result-box');
  const msSpan = card.querySelector('.run-ms');
  btn.disabled = true;
  resultBox.style.display = 'block';
  resultBox.className = 'tool-result-box';
  resultBox.textContent = 'Running…';
  msSpan.textContent = '';
  const t0 = performance.now();
  try {{
    const resp = await fetch(`/api/tools/${{name}}`, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(args),
    }});
    const ms = Math.round(performance.now() - t0);
    msSpan.textContent = ms + 'ms';
    const ct = resp.headers.get('content-type') || '';
    const data = ct.includes('application/json') ? await resp.json() : await resp.text();
    resultBox.textContent = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    if (resp.status >= 400) resultBox.classList.add('err');
  }} catch (err) {{
    resultBox.textContent = 'Error: ' + err.message;
    resultBox.classList.add('err');
    msSpan.textContent = Math.round(performance.now() - t0) + 'ms';
  }}
  btn.disabled = false;
}}

async function deleteTool(evt, name) {{
  evt.stopPropagation();
  if (!confirm(`Delete tool "${{name}}"?\n\nThis removes it from agent_router.py.`)) return;
  const btn = evt.currentTarget;
  btn.textContent = '…'; btn.disabled = true;
  try {{
    const r = await fetch(`/_apx/tools/${{encodeURIComponent(name)}}`, {{ method: 'DELETE' }});
    const d = await r.json();
    if (d.ok) {{ location.reload(); }}
    else {{ alert('Delete failed: ' + d.error); btn.textContent = '✕'; btn.disabled = false; }}
  }} catch (e) {{
    alert('Delete failed: ' + e.message); btn.textContent = '✕'; btn.disabled = false;
  }}
}}

// ── MCP URL ──
const mcpBar = document.getElementById('mcp-bar');
const mcpUrlEl = document.getElementById('mcp-url');
if (mcpUrlEl) {{
  // /mcp = stateless HTTP (Genie Code, AI Playground)
  // /mcp/sse = SSE transport (Claude Desktop, Cursor)
  mcpUrlEl.textContent = `${{window.location.origin}}/mcp`;
  mcpBar.style.display = 'flex';
}}
function copyMcpUrl() {{
  navigator.clipboard.writeText(`${{window.location.origin}}/mcp`).then(() => {{
    const ok = document.getElementById('copy-ok');
    ok.style.display = 'inline';
    setTimeout(() => ok.style.display = 'none', 1500);
  }});
}}
function copyMcpSseUrl() {{
  navigator.clipboard.writeText(`${{window.location.origin}}/mcp/sse`).then(() => {{
    const ok = document.getElementById('copy-sse-ok');
    if (ok) {{ ok.style.display = 'inline'; setTimeout(() => ok.style.display = 'none', 1500); }}
  }});
}}

// ── Tab switching ──
function switchTab(name, btn) {{
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.panel-tabs button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'eval' && !evalLoaded) loadEvalCases();
}}

// ── Eval tab ──
let evalRows = [];
let evalLoaded = false;

function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}

function renderEval() {{
  const el = document.getElementById('eval-cases');
  if (!evalRows.length) {{ el.innerHTML = '<div style="color:#444;font-size:12px;padding:20px 12px">No test cases. Add one below.</div>'; return; }}
  el.innerHTML = evalRows.map((r, i) => {{
    const dot = r.status === 'pass' ? '#4ade80' : r.status === 'fail' ? '#f87171' : r.status === 'running' ? '#facc15' : '#333';
    const anim = r.status === 'running' ? 'animation:pulse .8s infinite' : '';
    return `<div style="padding:8px 12px;border-bottom:1px solid #141414" data-idx="${{i}}">
      <div style="display:flex;align-items:flex-start;gap:8px">
        <span style="width:8px;height:8px;border-radius:50%;background:${{dot}};${{anim}};flex-shrink:0;margin-top:4px;display:inline-block"></span>
        <span style="font-size:12px;color:#ccc;flex:1;cursor:pointer" onclick="toggleEvalResp(this)">${{esc(r.question)}}</span>
      </div>
      ${{r.response ? `<div style="font-size:11px;color:#666;margin:4px 0 0 16px;display:none" class="eval-resp">${{esc(r.response.slice(0,200))}}${{r.response.length>200?'…':''}}</div>` : ''}}
    </div>`;
  }}).join('');
}}

function toggleEvalResp(el) {{
  const resp = el.parentElement.nextElementSibling;
  if (resp && resp.classList.contains('eval-resp')) resp.style.display = resp.style.display === 'none' ? '' : 'none';
}}

async function loadEvalCases() {{
  evalLoaded = true;
  try {{
    const r = await fetch('/_apx/eval/data');
    evalRows = await r.json();
    renderEval();
  }} catch(e) {{
    document.getElementById('eval-cases').innerHTML = '<div style="color:#f87171;font-size:12px;padding:12px">Failed to load: ' + e.message + '</div>';
  }}
}}

async function runEvalCase(i) {{
  const r = evalRows[i];
  r.status = 'running'; r.response = '';
  renderEval();
  try {{
    const resp = await fetch('/invocations', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{input: [{{role:'user', content:r.question}}]}}),
    }});
    const data = await resp.json();
    let text = '';
    try {{ text = data.output[0].content[0].text; }} catch {{ text = JSON.stringify(data); }}
    r.response = text;
    r.status = r.expected
      ? r.expected.split(/[,;]/).map(k=>k.trim().toLowerCase()).filter(Boolean).every(k=>text.toLowerCase().includes(k)) ? 'pass' : 'fail'
      : text.length > 10 ? 'pass' : 'fail';
  }} catch(e) {{ r.response = 'Error: ' + e.message; r.status = 'fail'; }}
  renderEval();
}}

document.getElementById('eval-run-all').addEventListener('click', async () => {{
  if (!evalLoaded) await loadEvalCases();
  const btn = document.getElementById('eval-run-all');
  const fill = document.getElementById('eval-progress-fill');
  const st   = document.getElementById('eval-status');
  btn.disabled = true;
  for (let i = 0; i < evalRows.length; i++) {{
    st.textContent = `${{i+1}}/${{evalRows.length}}`;
    fill.style.width = (i / evalRows.length * 100) + '%';
    await runEvalCase(i);
  }}
  fill.style.width = '100%';
  const passed = evalRows.filter(r => r.status === 'pass').length;
  st.textContent = `${{passed}}/${{evalRows.length}} passed`;
  btn.disabled = false;
}});

document.getElementById('eval-reset').addEventListener('click', () => {{
  evalRows.forEach(r => {{ r.status = 'pending'; r.response = ''; }});
  document.getElementById('eval-progress-fill').style.width = '0%';
  document.getElementById('eval-status').textContent = '';
  renderEval();
}});

document.getElementById('eval-add-btn').addEventListener('click', () => {{
  const q = document.getElementById('eval-add-q').value.trim();
  if (!q) return;
  evalRows.push({{question: q, expected: '', status: 'pending', response: ''}});
  document.getElementById('eval-add-q').value = '';
  renderEval();
}});

// ── State ──
const history = [];
let eventCounter = 0;
let events = [];
let eventsStarted = false;

function fmt(v) {{
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'string') return v.length > 600 ? v.slice(0, 600) + '\\n…' : v;
  const s = JSON.stringify(v, null, 2);
  return s.length > 1200 ? s.slice(0, 1200) + '\\n…' : s;
}}

// ── Events ──
function addEvent(type, title, subtitle, data) {{
  if (!eventsStarted) {{ eventsList.innerHTML = ''; eventsStarted = true; }}
  const num = eventCounter++;
  const ev = {{ num, type, title, subtitle, data }};
  events.push(ev);
  const div = document.createElement('div');
  div.className = 'event' + (type === 'tool-call' ? ' tool-call' : type === 'tool-result' ? ' tool-result' : type === 'tool-error' ? ' tool-error' : '');
  div.dataset.idx = events.length - 1;
  const icons = {{ user: '👤', assistant: '🤖', 'tool-call': '⚡', 'tool-result': '✓', 'tool-error': '✗' }};
  div.innerHTML = `<span class="event-num">#${{num}}</span><span class="event-icon">${{icons[type] || '•'}}</span>`
    + `<div class="event-body"><div class="event-title">${{title}}</div>`
    + (subtitle ? `<div class="event-sub">${{subtitle}}</div>` : '') + '</div>';
  div.onclick = () => showDetail(ev, div);
  eventsList.appendChild(div);
  eventsList.scrollTop = eventsList.scrollHeight;
  return ev;
}}

function showDetail(ev, el) {{
  document.querySelectorAll('.event.selected').forEach(e => e.classList.remove('selected'));
  if (el) el.classList.add('selected');
  detailTitle.textContent = `#${{ev.num}} ${{ev.type}}`;
  let html = '';
  if (ev.data) {{
    for (const [k, v] of Object.entries(ev.data)) {{
      html += `<div class="label">${{k}}</div><pre>${{typeof v === 'string' ? v : JSON.stringify(v, null, 2)}}</pre>`;
    }}
  }}
  detailBody.innerHTML = html;
  detailPanel.classList.add('open');
  // Auto-switch to events tab
  switchTab('events', document.querySelectorAll('.panel-tabs button')[1]);
}}

function closeDetail() {{
  detailPanel.classList.remove('open');
  document.querySelectorAll('.event.selected').forEach(e => e.classList.remove('selected'));
}}

// ── Chat ──
function addMsg(role, text, streaming) {{
  const div = document.createElement('div');
  div.className = `msg ${{role}}${{streaming ? ' streaming' : ''}}`;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}}

function addToolPills(trace) {{
  const container = document.createElement('div');
  container.className = 'tool-pills';
  for (const t of trace) {{
    const isErr = t.result && typeof t.result === 'object' && 'error' in t.result;
    const call = document.createElement('span');
    call.className = 'tool-pill call';
    call.innerHTML = `<span class="icon">⚡</span>${{t.name}}`;
    call.dataset.tip = JSON.stringify(t.args, null, 2);
    call.onmouseenter = showTip;
    call.onmouseleave = hideTip;
    const callEv = addEvent('tool-call', t.name, fmt(t.args).slice(0, 60), {{ arguments: t.args }});
    call.onclick = () => showDetail(callEv, eventsList.querySelector(`[data-idx="${{events.indexOf(callEv)}}"]`));
    container.appendChild(call);
    const res = document.createElement('span');
    res.className = `tool-pill ${{isErr ? 'error' : 'result'}}`;
    res.innerHTML = `<span class="icon">${{isErr ? '✗' : '✓'}}</span>${{t.name}}<span class="ms">${{t.ms}}ms</span>`;
    res.dataset.tip = fmt(t.result);
    res.onmouseenter = showTip;
    res.onmouseleave = hideTip;
    const resEv = addEvent(isErr ? 'tool-error' : 'tool-result', t.name, `${{t.ms}}ms`, {{ result: t.result }});
    res.onclick = () => showDetail(resEv, eventsList.querySelector(`[data-idx="${{events.indexOf(resEv)}}"]`));
    container.appendChild(res);
  }}
  chat.appendChild(container);
  chat.scrollTop = chat.scrollHeight;
}}

// ── Tooltip ──
function showTip(e) {{
  tooltip.textContent = e.target.closest('.tool-pill').dataset.tip;
  tooltip.classList.add('show');
  const r = e.target.getBoundingClientRect();
  tooltip.style.left = Math.min(r.left, window.innerWidth - 520) + 'px';
  tooltip.style.top = (r.bottom + 8) + 'px';
}}
function hideTip() {{ tooltip.classList.remove('show'); }}

// ── Input ──
inputEl.addEventListener('input', () => {{
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
}});
inputEl.addEventListener('keydown', e => {{
  if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); form.requestSubmit(); }}
}});

// ── Submit ──
form.addEventListener('submit', async e => {{
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;

  addMsg('user', text);
  addEvent('user', text.slice(0, 80), null, {{ content: text }});
  history.push({{ role: 'user', content: text }});

  const assistantDiv = addMsg('assistant', '', true);
  let full = '';
  let pendingTrace = null;

  try {{
    const res = await fetch('/invocations', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ input: history, stream: true }}),
    }});
    if (!res.ok) throw new Error(`${{res.status}} ${{await res.text()}}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {{
      const {{ done, value }} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {{ stream: true }});
      const lines = buf.split('\\n');
      buf = lines.pop();
      let eventType = '';
      for (const line of lines) {{
        if (line.startsWith('event: ')) eventType = line.slice(7).trim();
        else if (line.startsWith('data: ')) {{
          try {{
            const payload = JSON.parse(line.slice(6));
            if (eventType === 'output_text.delta' && payload.text) {{
              full += payload.text;
              assistantDiv.textContent = full;
              chat.scrollTop = chat.scrollHeight;
            }} else if (eventType === 'tool.trace') {{
              pendingTrace = payload;
            }}
          }} catch {{}}
        }}
      }}
    }}
  }} catch (err) {{
    full = `Error: ${{err.message}}`;
    assistantDiv.textContent = full;
  }}

  assistantDiv.classList.remove('streaming');
  if (pendingTrace && pendingTrace.length) {{
    addToolPills(pendingTrace);
  }}
  addEvent('assistant', full.slice(0, 80) + (full.length > 80 ? '…' : ''), null, {{ content: full }});
  history.push({{ role: 'assistant', content: full }});
  sendBtn.disabled = false;
  inputEl.focus();
}});

// ── Resizable panel ──
const rightPanel = document.getElementById('right-panel');
const handle = document.getElementById('resize-handle');
let resizing = false;
handle.addEventListener('mousedown', () => {{ resizing = true; document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none'; }});
document.addEventListener('mousemove', e => {{
  if (!resizing) return;
  const w = Math.max(280, Math.min(700, window.innerWidth - e.clientX));
  rightPanel.style.width = w + 'px';
}});
document.addEventListener('mouseup', () => {{ resizing = false; document.body.style.cursor = ''; document.body.style.userSelect = ''; }});

inputEl.focus();
</script>
{_deploy_overlay_html()}
</body>
</html>"""


def _build_apx_openapi_spec(ctx: AgentContext | None, api_prefix: str = "/api") -> dict[str, Any]:
    """Build an OpenAPI 3.1 spec containing only tool endpoints with dep-stripped schemas.

    This is what the LLM sees — not the full FastAPI route signatures (which include
    injected deps like WorkspaceClient). Used by /_apx/openapi.json and Scalar.
    """
    if ctx is None:
        return {
            "openapi": "3.1.0",
            "info": {"title": "Agent Tools", "version": "0.0.0"},
            "paths": {},
        }

    paths: dict[str, Any] = {}
    for t in ctx.tools:
        if t.name == "create_tool":
            continue  # meta-tool — no real FastAPI route
        if t.sub_agent_url:
            request_schema: dict[str, Any] = {
                "type": "object",
                "properties": {
                    "input": {"type": "string", "description": "Free-text input for the sub-agent"}
                },
            }
            tag = "Remote"
        else:
            request_schema = t.input_schema or {"type": "object", "properties": {}}
            tag = "Local"

        paths[f"{api_prefix}/tools/{t.name}"] = {
            "post": {
                "operationId": t.name,
                "summary": t.name,
                "description": t.description or "",
                "tags": [tag],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": request_schema}},
                },
                "responses": {
                    "200": {
                        "description": "Tool result",
                        "content": {
                            "application/json": {
                                "schema": t.output_schema or {"type": "object"}
                            }
                        },
                    }
                },
            }
        }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": ctx.config.name,
            "description": ctx.config.description or "",
            "version": "0.0.0",
        },
        "paths": paths,
    }


def _render_tools_ui(ctx: AgentContext | None) -> str:
    """Return a Scalar API reference page scoped to the agent's tool endpoints.

    Uses /_apx/openapi.json (dep-stripped model serving schemas) so the display matches
    exactly what the LLM sees — not the full FastAPI route signatures.
    """
    import json as _json

    # Detect AppClient alias for the New Tool wizard
    _ws_type = "AppClient"
    _ar_path = _find_agent_router_path()
    if _ar_path and _ar_path.exists():
        import re as _re
        _src = _ar_path.read_text()
        _m = _re.search(r"^(\w+)\s*=\s*Dependencies\.Client", _src, _re.MULTILINE)
        if _m:
            _ws_type = _m.group(1)
    ws_type_js = _json.dumps(_ws_type)

    not_configured = ctx is None
    banner = (
        '<div id="apx-banner"><strong>⚠ Agent not configured</strong> — add '
        '<code>[tool.apx.agent]</code> to <code>pyproject.toml</code> and '
        'create <code>agent_router.py</code>, then restart.</div>'
        if not_configured else ""
    )

    # VS config strip — shown when vector_search_index is set in pyproject.toml
    vs_strip = ""
    if ctx and ctx.config.vector_search_index:
        vs_strip = (
            f'<div id="apx-vs-strip">'
            f'<span class="vs-dot">●</span> Vector Search: '
            f'<code>{ctx.config.vector_search_index}</code>'
            f'</div>'
        )
    elif ctx:
        vs_strip = (
            '<div id="apx-vs-strip" class="vs-not-set">'
            'No <code>vector_search_index</code> configured — '
            '<a href="/_apx/probe">discover indexes</a> or set '
            '<code>vector_search_index = "catalog.schema.index"</code> in '
            '<code>[tool.apx.agent]</code>'
            '</div>'
        )
    scalar_config = _json.dumps({
        "theme": "kepler",
        "darkMode": True,
        "hideModels": True,
        "hideDownloadButton": True,
        "defaultHttpClient": {"targetKey": "shell", "clientKey": "curl"},
    })

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tools — APX Dev</title>
<style>
  #apx-header {{
    position: fixed; top: 0; left: 0; right: 0; z-index: 1000;
    background: #111; border-bottom: 1px solid #2a2a2a;
  }}
  #apx-nav {{
    padding: 10px 16px; display: flex; align-items: center; gap: 10px; height: 44px;
  }}
  .badge {{ background: #1e3a5f; color: #60b0ff; font-size: 11px; font-weight: 600;
            padding: 2px 8px; border-radius: 4px; letter-spacing: .5px; text-transform: uppercase; }}
  #apx-nav h1 {{ font-size: 15px; font-weight: 600; color: #fff; }}
  nav {{ margin-left: auto; display: flex; gap: 4px; }}
  nav a {{ font-size: 12px; color: #888; text-decoration: none; padding: 3px 10px;
           border-radius: 5px; border: 1px solid transparent; }}
  nav a:hover {{ color: #ccc; border-color: #333; }}
  nav a.active {{ color: #60b0ff; background: #0d1f38; border-color: #1e3a5f; }}
  #apx-banner {{
    background: #2a1a00; border-bottom: 1px solid #5a3a00; color: #ffb84d;
    padding: 10px 16px; font-size: 13px; line-height: 1.5;
  }}
  #apx-banner code {{ background: #1a1000; padding: 1px 5px; border-radius: 3px;
                      font-family: monospace; font-size: 12px; }}
  #apx-vs-strip {{
    background: #07150a; border-bottom: 1px solid #14532d; color: #4ade80;
    padding: 7px 16px; font-size: 12px; font-family: monospace;
  }}
  #apx-vs-strip.vs-not-set {{
    background: #111; border-bottom: 1px solid #2a2a2a; color: #555;
  }}
  #apx-vs-strip .vs-dot {{ margin-right: 6px; }}
  #apx-vs-strip code {{ font-family: monospace; font-size: 11px; }}
  #apx-vs-strip a {{ color: #60b0ff; text-decoration: none; }}
  #apx-vs-strip a:hover {{ text-decoration: underline; }}
  #btn-new-tool {{ background: #1a3a1a; color: #4ade80; border: 1px solid #14532d;
                   border-radius: 6px; padding: 4px 12px; font-size: 12px; font-weight: 600;
                   cursor: pointer; margin-left: 8px; white-space: nowrap; }}
  #btn-new-tool:hover {{ background: #14532d; }}
  /* ── New Tool modal ── */
  #modal-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.7);
                    display: none; align-items: center; justify-content: center; z-index: 1001; }}
  #modal-overlay.open {{ display: flex; }}
  #modal {{ background: #141414; border: 1px solid #2a2a2a; border-radius: 10px;
            width: min(880px, 95vw); max-height: 90vh; display: flex;
            flex-direction: column; overflow: hidden; }}
  #modal-head {{ padding: 16px 20px; border-bottom: 1px solid #1e1e1e;
                 display: flex; align-items: center; justify-content: space-between;
                 flex-shrink: 0; }}
  #modal-head h2 {{ font-size: 14px; font-weight: 600; }}
  #modal-close {{ background: none; border: none; color: #555; font-size: 18px;
                  cursor: pointer; line-height: 1; padding: 2px 6px; }}
  #modal-close:hover {{ color: #ccc; }}
  #modal-body {{ display: grid; grid-template-columns: 1fr 260px;
                 overflow: hidden; flex: 1; min-height: 0; }}
  #modal-form {{ padding: 20px; display: flex; flex-direction: column; gap: 14px;
                 overflow-y: auto; }}
  #schema-panel {{ border-left: 1px solid #1e1e1e; overflow-y: auto;
                   background: #0d0d0d; display: flex; flex-direction: column; }}
  #schema-panel-head {{ padding: 10px 12px; border-bottom: 1px solid #1e1e1e;
                        font-size: 11px; font-weight: 600; color: #555;
                        text-transform: uppercase; letter-spacing: .4px;
                        position: sticky; top: 0; background: #0d0d0d; z-index: 1; }}
  #schema-panel-head span {{ color: #333; font-weight: 400; text-transform: none;
                              letter-spacing: 0; margin-left: 4px; font-size: 10px; }}
  #schema-tables {{ padding: 4px 0; }}
  .st-table-name {{ display: flex; align-items: center; gap: 5px;
                    padding: 5px 12px; font-size: 11px; font-family: monospace;
                    color: #666; cursor: pointer; user-select: none; }}
  .st-table-name:hover, .st-table-name.open {{ color: #ccc; }}
  .st-table-name .arrow {{ font-size: 8px; color: #333; transition: transform .12s; width: 8px; }}
  .st-table-name.open .arrow {{ transform: rotate(90deg); color: #555; }}
  .st-cols {{ display: none; padding-bottom: 2px; }}
  .st-cols.open {{ display: block; }}
  .st-col {{ display: flex; align-items: baseline; gap: 0;
             padding: 2px 12px 2px 22px; cursor: pointer; }}
  .st-col:hover {{ background: #141414; }}
  .st-col:hover .col-name {{ color: #60b0ff; }}
  .col-name {{ font-size: 11px; font-family: monospace; color: #999; flex: 1; }}
  .col-type {{ font-size: 10px; color: #333; font-family: monospace; padding-left: 6px; }}
  .schema-msg {{ padding: 14px 12px; font-size: 11px; color: #444; }}
  .field {{ display: flex; flex-direction: column; gap: 5px; }}
  .field label {{ font-size: 11px; font-weight: 600; color: #888;
                  text-transform: uppercase; letter-spacing: .4px; }}
  .field input, .field textarea, .field select {{
    background: #0d0d0d; border: 1px solid #2a2a2a; color: #e8e8e8;
    border-radius: 6px; padding: 7px 10px; font-size: 13px; font-family: monospace;
    outline: none; resize: vertical; }}
  .field input:focus, .field textarea:focus, .field select:focus {{ border-color: #3a7bd5; }}
  .field select {{ cursor: pointer; }}
  #param-rows {{ display: flex; flex-direction: column; gap: 6px; }}
  .param-row-form {{ display: grid; grid-template-columns: 1fr 100px 1fr auto;
                     gap: 6px; align-items: center; }}
  .param-row-form input, .param-row-form select {{
    background: #0d0d0d; border: 1px solid #2a2a2a; color: #e8e8e8;
    border-radius: 5px; padding: 5px 8px; font-size: 12px; font-family: monospace; outline: none; }}
  .param-row-form input:focus, .param-row-form select:focus {{ border-color: #3a7bd5; }}
  .btn-rm-param {{ background: none; border: none; color: #555; cursor: pointer;
                   font-size: 16px; line-height: 1; padding: 2px 4px; }}
  .btn-rm-param:hover {{ color: #f87171; }}
  #btn-add-param {{ background: none; border: 1px dashed #2a2a2a; color: #555;
                    border-radius: 6px; padding: 6px; font-size: 12px;
                    cursor: pointer; text-align: center; margin-top: 2px; }}
  #btn-add-param:hover {{ border-color: #3a7bd5; color: #60b0ff; }}
  #modal-preview {{ background: #0d0d0d; border: 1px solid #1e1e1e; border-radius: 6px;
                    padding: 12px; font-size: 12px; font-family: monospace;
                    color: #a5f3fc; white-space: pre; overflow-x: auto; line-height: 1.5; }}
  #modal-foot {{ padding: 14px 20px; border-top: 1px solid #1e1e1e;
                 display: flex; justify-content: flex-end; gap: 8px; align-items: center; }}
  #modal-status {{ flex: 1; font-size: 12px; }}
  #modal-status.ok {{ color: #4ade80; }}
  #modal-status.err {{ color: #f87171; }}
  #btn-insert {{ background: #2563eb; color: #fff; border: none; border-radius: 6px;
                 padding: 7px 18px; font-size: 13px; cursor: pointer; font-weight: 500; }}
  #btn-insert:hover {{ background: #1d4ed8; }}
  #btn-insert:disabled {{ opacity: .5; cursor: default; }}
  #btn-cancel {{ background: transparent; color: #888; border: 1px solid #333;
                 border-radius: 6px; padding: 7px 14px; font-size: 13px; cursor: pointer; }}
  #btn-cancel:hover {{ color: #ccc; border-color: #555; }}
  #f-prompt-wrap {{ display: flex; gap: 8px; align-items: flex-start; }}
  #f-prompt {{ flex: 1; }}
  #btn-suggest {{ background: #1a1a2e; color: #a78bfa; border: 1px solid #3730a3;
                  border-radius: 6px; padding: 7px 12px; font-size: 12px; font-weight: 600;
                  cursor: pointer; white-space: nowrap; align-self: flex-start; }}
  #btn-suggest:hover {{ background: #2d1b69; }}
  #btn-suggest:disabled {{ opacity: .5; cursor: default; }}
</style>
</head>
<body>
<div id="apx-header">
  <div id="apx-nav">
    <span class="badge">APX dev</span>
    <h1>Tools</h1>
    <nav>
      <a href="/_apx/agent">Chat</a>
      <a href="/_apx/tools" class="active">Tools</a>
      <a href="/_apx/edit">Edit</a>
      <a href="/_apx/probe">Probe</a>
      <a href="/_apx/setup">Setup</a>
      <a href="/_apx/eval">Eval</a>
      <a href="/_apx/wizard">Wizard</a>
    </nav>
    <button id="btn-new-tool">+ New Tool</button>
    <button id="btn-deploy">Deploy ▶</button>
  </div>
  {banner}
  {vs_strip}
</div>
<script>
  // Keep Scalar's content below the fixed APX header
  const apxHeader = document.getElementById('apx-header');
  function syncPadding() {{ document.body.style.paddingTop = apxHeader.offsetHeight + 'px'; }}
  syncPadding();
  new ResizeObserver(syncPadding).observe(apxHeader);
</script>

<!-- New Tool modal -->
<div id="modal-overlay">
  <div id="modal">
    <div id="modal-head">
      <h2>New Tool</h2>
      <button id="modal-close">✕</button>
    </div>
    <div id="modal-body">
      <!-- left: form -->
      <div id="modal-form">
        <div class="field">
          <label>Describe your tool <span style="font-weight:400;text-transform:none;color:#444">— AI fills the fields below</span></label>
          <div id="f-prompt-wrap">
            <textarea id="f-prompt" rows="2" placeholder="e.g. get a customer's monthly energy usage for a given billing period"></textarea>
            <button id="btn-suggest">✨ Suggest</button>
          </div>
        </div>
        <div class="field">
          <label>Function name</label>
          <input id="f-name" type="text" placeholder="my_tool" spellcheck="false">
        </div>
        <div class="field">
          <label>Description <span style="font-weight:400;text-transform:none;color:#444">(shown to the model)</span></label>
          <textarea id="f-desc" rows="2" placeholder="What this tool does for the agent"></textarea>
        </div>
        <div class="field">
          <label>Parameters</label>
          <div id="param-rows"></div>
          <button id="btn-add-param">+ Add parameter</button>
        </div>
        <div class="field">
          <label>Returns</label>
          <select id="f-return">
            <option value="str">str</option>
            <option value="list[str]">list[str]</option>
            <option value="dict[str, Any]">dict[str, Any]</option>
            <option value="int">int</option>
            <option value="float">float</option>
            <option value="bool">bool</option>
          </select>
        </div>
        <div class="field" id="f-body-wrap" style="display:none">
          <label>Body <span style="font-weight:400;text-transform:none;color:#444">(generated — edit before inserting)</span></label>
          <textarea id="f-body" rows="8" placeholder="# implementation will appear here after ✨ Suggest" spellcheck="false" style="font-size:11px;line-height:1.55"></textarea>
        </div>
        <div class="field">
          <label>Signature preview</label>
          <pre id="modal-preview"></pre>
        </div>
      </div>
      <!-- right: schema browser -->
      <div id="schema-panel">
        <div id="schema-panel-head">Tables<span id="schema-panel-subtitle"></span></div>
        <div id="schema-tables"><p class="schema-msg">Loading…</p></div>
      </div>
    </div>
    <div id="modal-foot">
      <span id="modal-status"></span>
      <button id="btn-cancel">Cancel</button>
      <button id="btn-insert">Insert Tool</button>
    </div>
  </div>
</div>

<script>
const WS_TYPE = {ws_type_js};
const TYPES   = ['str','int','float','bool','list[str]','dict[str, Any]'];
const overlay = document.getElementById('modal-overlay');

// Track which p-name input was last focused so column clicks can fill it
let _lastParamInput = null;

function typeSelect(val='str') {{
  return '<select class="p-type">' + TYPES.map(t => `<option${{t===val?' selected':''}}>${{t}}</option>`).join('') + '</select>';
}}

function addParamRow(name='', type='str', desc='') {{
  const row = document.createElement('div');
  row.className = 'param-row-form';
  row.innerHTML = `<input class="p-name" placeholder="param_name" value="${{name}}" spellcheck="false">`
    + typeSelect(type)
    + `<input class="p-desc" placeholder="description" value="${{desc}}">`
    + `<button class="btn-rm-param" title="Remove">✕</button>`;
  row.querySelector('.btn-rm-param').onclick = () => {{ row.remove(); updatePreview(); }};
  row.querySelectorAll('input,select').forEach(el => el.addEventListener('input', updatePreview));
  const pName = row.querySelector('.p-name');
  pName.addEventListener('focus', () => {{ _lastParamInput = pName; }});
  document.getElementById('param-rows').appendChild(row);
  updatePreview();
}}

function collectParams() {{
  return [...document.querySelectorAll('.param-row-form')].map(r => ({{
    name: r.querySelector('.p-name').value.trim(),
    type: r.querySelector('.p-type').value,
    desc: r.querySelector('.p-desc').value.trim(),
  }})).filter(p => p.name);
}}

function buildPreview() {{
  const name = (document.getElementById('f-name').value.trim() || 'my_tool').replace(/\\W/g,'_');
  const desc = document.getElementById('f-desc').value.trim() || 'Describe what this tool does.';
  const ret  = document.getElementById('f-return').value;
  const params = collectParams();
  const sigParams = params.map(p => `${{p.name}}: ${{p.type}}`).join(', ');
  const sep = sigParams ? ', ' : '';
  const sig = `def ${{name}}(${{sigParams}}${{sep}}ws: ${{WS_TYPE}}) -> ${{ret}}:`;
  const docLines = [desc];
  params.forEach(p => {{ if (p.desc) docLines.push(`    ${{p.name}}: ${{p.desc}}`); }});
  const docstring = docLines.length > 1
    ? `    {chr(34)*3}${{docLines[0]}}\\n${{docLines.slice(1).join('\\n')}}\\n    {chr(34)*3}`
    : `    {chr(34)*3}${{docLines[0]}}{chr(34)*3}`;
  return `${{sig}}\\n${{docstring}}\\n    # TODO: implement your tool\\n    pass`;
}}

function updatePreview() {{
  document.getElementById('modal-preview').textContent = buildPreview();
}}

// ── Schema panel ──────────────────────────────────────────────────────────────
let _schemaCache = null;

function renderSchema(data) {{
  const subtitle = document.getElementById('schema-panel-subtitle');
  const container = document.getElementById('schema-tables');
  if (!data.ok) {{
    subtitle.textContent = '';
    container.innerHTML = `<p class="schema-msg">${{data.error || 'Unavailable'}}</p>`;
    return;
  }}
  subtitle.textContent = ` ${{data.schema}}`;
  const tables = data.tables || {{}};
  if (!Object.keys(tables).length) {{
    container.innerHTML = '<p class="schema-msg">No tables found</p>';
    return;
  }}
  container.innerHTML = Object.entries(tables).map(([tbl, cols]) => `
    <div class="st-table">
      <div class="st-table-name" data-tbl="${{tbl}}">
        <span class="arrow">▶</span>${{tbl}}
      </div>
      <div class="st-cols" id="cols-${{tbl}}">
        ${{cols.map(c => `<div class="st-col" data-col="${{c.name}}" data-type="${{c.type}}">
          <span class="col-name">${{c.name}}</span>
          <span class="col-type">${{c.type}}</span>
        </div>`).join('')}}
      </div>
    </div>`).join('');

  // Toggle table expand
  container.querySelectorAll('.st-table-name').forEach(el => {{
    el.addEventListener('click', () => {{
      el.classList.toggle('open');
      document.getElementById('cols-' + el.dataset.tbl).classList.toggle('open');
    }});
  }});

  // Click column → fill last focused param name, or add new param row
  container.querySelectorAll('.st-col').forEach(el => {{
    el.addEventListener('click', () => {{
      const col = el.dataset.col;
      if (_lastParamInput && document.getElementById('modal-form').contains(_lastParamInput)) {{
        _lastParamInput.value = col;
        _lastParamInput.dispatchEvent(new Event('input'));
        _lastParamInput.focus();
      }} else {{
        addParamRow(col, 'str', '');
        // focus the new name input
        const rows = document.querySelectorAll('.param-row-form');
        const last = rows[rows.length - 1];
        if (last) last.querySelector('.p-name').focus();
      }}
    }});
  }});
}}

async function loadSchema() {{
  if (_schemaCache) {{ renderSchema(_schemaCache); return; }}
  try {{
    const r = await fetch('/_apx/tools/schema');
    _schemaCache = await r.json();
    renderSchema(_schemaCache);
  }} catch (e) {{
    document.getElementById('schema-tables').innerHTML =
      `<p class="schema-msg">Could not load schema</p>`;
  }}
}}

document.getElementById('btn-new-tool').addEventListener('click', () => {{
  document.getElementById('f-prompt').value = '';
  document.getElementById('f-name').value = '';
  document.getElementById('f-desc').value = '';
  document.getElementById('f-return').value = 'str';
  document.getElementById('f-body').value = '';
  document.getElementById('f-body-wrap').style.display = 'none';
  document.getElementById('param-rows').innerHTML = '';
  document.getElementById('modal-status').textContent = '';
  document.getElementById('modal-status').className = '';
  document.getElementById('btn-suggest').disabled = false;
  document.getElementById('btn-suggest').textContent = '✨ Suggest';
  _lastParamInput = null;
  updatePreview();
  overlay.classList.add('open');
  document.getElementById('f-prompt').focus();
  loadSchema();
}});
document.getElementById('modal-close').onclick =
document.getElementById('btn-cancel').onclick = () => overlay.classList.remove('open');
overlay.addEventListener('click', e => {{ if (e.target === overlay) overlay.classList.remove('open'); }});
document.getElementById('btn-add-param').onclick = () => addParamRow();
document.getElementById('f-name').addEventListener('input', updatePreview);
document.getElementById('f-desc').addEventListener('input', updatePreview);
document.getElementById('f-return').addEventListener('input', updatePreview);

document.getElementById('btn-suggest').addEventListener('click', async () => {{
  const prompt = document.getElementById('f-prompt').value.trim();
  if (!prompt) return;
  const btn    = document.getElementById('btn-suggest');
  const status = document.getElementById('modal-status');
  btn.disabled = true; btn.textContent = '…';
  status.textContent = 'Suggesting…'; status.className = '';
  try {{
    const r = await fetch('/_apx/tools/suggest', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ prompt }}),
    }});
    const d = await r.json();
    if (d.ok) {{
      const s = d.spec;
      document.getElementById('f-name').value = s.name || '';
      document.getElementById('f-desc').value = s.description || '';
      if (s.returns) document.getElementById('f-return').value = s.returns;
      document.getElementById('param-rows').innerHTML = '';
      (s.params || []).forEach(p => addParamRow(p.name, p.type || 'str', p.desc || ''));
      const bodyWrap = document.getElementById('f-body-wrap');
      if (s.body) {{
        document.getElementById('f-body').value = s.body;
        bodyWrap.style.display = '';
      }} else {{
        document.getElementById('f-body').value = '';
        bodyWrap.style.display = 'none';
      }}
      updatePreview();
      status.textContent = ''; status.className = '';
    }} else {{
      status.textContent = '✗ ' + d.error; status.className = 'err';
    }}
  }} catch (e) {{
    status.textContent = '✗ ' + e.message; status.className = 'err';
  }}
  btn.disabled = false; btn.textContent = '✨ Suggest';
}});

document.getElementById('btn-insert').addEventListener('click', async () => {{
  const name   = (document.getElementById('f-name').value.trim() || 'my_tool').replace(/\\W/g,'_');
  const desc   = document.getElementById('f-desc').value.trim();
  const ret    = document.getElementById('f-return').value;
  const params = collectParams();
  const btn    = document.getElementById('btn-insert');
  const status = document.getElementById('modal-status');
  btn.disabled = true;
  status.textContent = 'Saving…'; status.className = '';
  try {{
    const r = await fetch('/_apx/tools/new', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ name, description: desc, params, returns: ret, body: document.getElementById('f-body').value || null }}),
    }});
    const d = await r.json();
    if (d.ok) {{
      status.textContent = '✓ Tool added — reloading…'; status.className = 'ok';
      setTimeout(() => location.reload(), 800);
    }} else {{
      status.textContent = '✗ ' + d.error; status.className = 'err';
      btn.disabled = false;
    }}
  }} catch (e) {{
    status.textContent = '✗ ' + e.message; status.className = 'err';
    btn.disabled = false;
  }}
}});
</script>

<script id="api-reference" data-url="/_apx/openapi.json"
  data-configuration='{scalar_config}'></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
{_deploy_overlay_html()}
</body>
</html>"""


