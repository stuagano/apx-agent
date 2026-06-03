"""Dev UI — /_apx/agent unified shell, chat interface, OpenAPI spec builder, and /_apx/tools inspector."""

from __future__ import annotations

import os
from typing import Any

from ._models import AgentContext
from ._ui_edit import _find_agent_router_path
from ._ui_nav import _apx_nav_css, _apx_nav_html, _apx_nav_links, _deploy_overlay_html


# Tabs exposed by the unified shell at /_apx/agent. Each entry is
# (slug, label, iframe URL). The shell defaults to the first tab. To
# add a tab, append here — the shell auto-renders it and the URL
# fragment-router handles selection.
_UNIFIED_TABS: tuple[tuple[str, str, str], ...] = (
    ("chat", "Chat", "/_apx/chat"),
    ("edit", "Edit", "/_apx/edit"),
    ("eval", "Eval", "/_apx/eval"),
    # "setup" is intentionally not a shell tab — its data-source + tool
    # generation flow is reached from the Edit page's "✨ From data" modal.
    ("probe", "Probe", "/_apx/probe"),
)


def _render_unified_shell(ctx: AgentContext | None) -> str:
    """Render the tabbed shell that hosts every /_apx/* page in one iframe.

    Each tab swaps the iframe src + updates the URL hash so bookmarks +
    browser back/forward work. The inner pages detect iframe context via
    ``window.self !== window.top`` and hide their own nav bar (see
    ``_apx_nav_html``) so the shell's tab strip is the only nav.
    """
    agent_name = ctx.config.name if ctx else "Agent"
    agent_desc = ctx.config.description if ctx else ""

    tab_buttons = "".join(
        f'<button class="tab" data-tab="{slug}" data-src="{src}">{label}</button>'
        for slug, label, src in _UNIFIED_TABS
    )
    default_slug = _UNIFIED_TABS[0][0]
    default_src = _UNIFIED_TABS[0][2]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{agent_name} · APX dev</title>
  <style>
    :root {{
      --bg: #0a0a0a; --panel: #111; --border: #2a2a2a; --border-strong: #333;
      --text: #e5e7eb; --text-muted: #888; --accent: #60b0ff;
      --accent-bg: #0d1f38; --accent-border: #1e3a5f;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; height: 100%; background: var(--bg); color: var(--text);
                  font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; font-size: 13px; }}
    header {{ height: 52px; padding: 0 16px; display: flex; align-items: center;
              gap: 16px; background: var(--panel); border-bottom: 1px solid var(--border); }}
    .badge {{ background: var(--accent-bg); color: var(--accent); font-size: 11px;
              font-weight: 600; padding: 3px 8px; border-radius: 4px; letter-spacing: .5px;
              text-transform: uppercase; }}
    .title {{ display: flex; flex-direction: column; gap: 1px; }}
    .agent-name {{ font-weight: 600; font-size: 14px; }}
    .agent-desc {{ color: var(--text-muted); font-size: 11px; max-width: 480px;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .tabs {{ display: flex; gap: 2px; margin-left: auto; }}
    .tab {{ font: inherit; color: var(--text-muted); background: transparent;
            border: 1px solid transparent; border-radius: 5px; padding: 5px 12px;
            cursor: pointer; font-size: 12px; }}
    .tab:hover {{ color: var(--text); border-color: var(--border-strong); }}
    .tab.active {{ color: var(--accent); background: var(--accent-bg);
                   border-color: var(--accent-border); }}
    .util-btn {{ font: inherit; color: var(--text-muted); background: transparent;
                 border: 1px solid var(--border-strong); border-radius: 5px;
                 padding: 5px 10px; cursor: pointer; font-size: 12px;
                 display: inline-flex; align-items: center; gap: 6px; }}
    .util-btn:hover {{ color: var(--text); border-color: #555; }}
    .util-btn .ico {{ font-size: 13px; line-height: 1; }}
    main {{ height: calc(100% - 52px); background: var(--bg); }}
    iframe {{ width: 100%; height: 100%; border: 0; background: var(--bg); }}
    /* Topology pop-out modal */
    #topo-overlay {{ display: none; position: fixed; inset: 0; z-index: 1500;
                     background: rgba(0,0,0,.7); align-items: center; justify-content: center; }}
    #topo-overlay.open {{ display: flex; }}
    #topo-modal {{ background: var(--panel); border: 1px solid var(--border);
                   border-radius: 10px; width: min(1100px, 95vw);
                   height: min(720px, 90vh); display: flex; flex-direction: column;
                   overflow: hidden; box-shadow: 0 20px 60px rgba(0,0,0,.5); }}
    #topo-head {{ height: 44px; padding: 0 14px; display: flex; align-items: center;
                  gap: 10px; border-bottom: 1px solid var(--border); flex-shrink: 0; }}
    #topo-head h2 {{ margin: 0; font-size: 13px; font-weight: 600; color: var(--text); }}
    #topo-head .badge {{ font-size: 10px; padding: 2px 7px; }}
    #topo-close {{ margin-left: auto; background: none; border: none;
                   color: var(--text-muted); font-size: 20px; cursor: pointer;
                   padding: 0 6px; line-height: 1; }}
    #topo-close:hover {{ color: var(--text); }}
    #topo-body {{ flex: 1; background: var(--bg); min-height: 0; }}
    #topo-frame {{ width: 100%; height: 100%; border: 0; }}
  </style>
</head>
<body>
  <header>
    <span class="badge">APX dev</span>
    <div class="title">
      <div class="agent-name">{agent_name}</div>
      <div class="agent-desc">{agent_desc}</div>
    </div>
    <div class="tabs">{tab_buttons}</div>
    <button id="topo-open" class="util-btn" title="Open topology graph">
      <span class="ico">⧉</span> Topology
    </button>
    <a href="/_apx/traces" target="_blank" class="util-btn" title="Browse trace history">
      <span class="ico">⏱</span> Traces
    </a>
  </header>
  <main><iframe id="dash-frame" src="{default_src}"></iframe></main>
  <div id="topo-overlay" role="dialog" aria-modal="true" aria-labelledby="topo-title">
    <div id="topo-modal">
      <div id="topo-head">
        <span class="badge">APX</span>
        <h2 id="topo-title">Agent topology</h2>
        <button id="topo-close" aria-label="Close">×</button>
      </div>
      <div id="topo-body"><iframe id="topo-frame" title="Agent topology"></iframe></div>
    </div>
  </div>
  <script>
    (function () {{
      const frame = document.getElementById("dash-frame");
      const tabs = document.querySelectorAll(".tab");
      function selectTab(slug) {{
        const btn = document.querySelector('.tab[data-tab="' + slug + '"]');
        if (!btn) return;
        tabs.forEach((t) => t.classList.remove("active"));
        btn.classList.add("active");
        if (frame.src.replace(location.origin, "") !== btn.dataset.src) {{
          frame.src = btn.dataset.src;
        }}
        history.replaceState(null, "", "#" + slug);
      }}
      tabs.forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));
      window.addEventListener("hashchange", () => {{
        const slug = (location.hash || "#{default_slug}").slice(1);
        selectTab(slug);
      }});
      const initial = (location.hash || "#{default_slug}").slice(1);
      selectTab(initial);
    }})();
    (function () {{
      const overlay = document.getElementById("topo-overlay");
      const openBtn = document.getElementById("topo-open");
      const closeBtn = document.getElementById("topo-close");
      const tframe = document.getElementById("topo-frame");
      let loaded = false;
      function open() {{
        if (!loaded) {{ tframe.src = "/_apx/topology"; loaded = true; }}
        overlay.classList.add("open");
      }}
      function close() {{ overlay.classList.remove("open"); }}
      openBtn.addEventListener("click", open);
      closeBtn.addEventListener("click", close);
      overlay.addEventListener("click", (e) => {{ if (e.target === overlay) close(); }});
      document.addEventListener("keydown", (e) => {{
        if (e.key === "Escape" && overlay.classList.contains("open")) close();
      }});
    }})();
  </script>
</body>
</html>
"""


def _render_eval_landing(
    cases: list[dict],
    loaded_path: str | None,
    load_error: str | None,
) -> str:
    """Eval page — run eval cases against the live agent with LLM-as-judge scoring."""
    import html as _html
    import json as _json

    cases_json = _json.dumps(cases)
    file_label = _html.escape(loaded_path) if loaded_path else "(no evals.json)"
    case_count = len(cases)

    empty_banner = ""
    if load_error:
        empty_banner = (
            f'<div class="banner banner-err"><strong>Couldn\'t load eval cases</strong> &middot; {_html.escape(load_error)}</div>'
        )
    elif not cases:
        empty_banner = (
            '<div class="banner banner-info"><strong>No eval cases yet.</strong> '
            'Add a question below to get started.</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Eval &middot; APX dev</title>
  <style>
    :root {{ --bg: #0a0a0a; --panel: #111; --border: #2a2a2a; --text: #e5e7eb;
             --muted: #888; --accent: #60b0ff; --accent-bg: #0d1f38; --accent-border: #1e3a5f; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text);
            font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; line-height: 1.5; }}
    .container {{ max-width: 900px; margin: 0 auto; padding: 24px 20px; }}
    h1 {{ font-size: 17px; font-weight: 600; margin: 0 0 4px; }}
    .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 16px; }}
    .meta code {{ background: #1a1a1a; padding: 1px 5px; border-radius: 3px; color: #ccc; }}
    .toolbar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 16px; }}
    .btn {{ padding: 5px 14px; border-radius: 5px; font-size: 12px; cursor: pointer; border: 1px solid; }}
    .btn-run {{ background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); }}
    .btn-run:disabled {{ opacity: .4; cursor: default; }}
    .btn-reset {{ background: transparent; color: var(--muted); border-color: #333; }}
    #status {{ font-size: 11px; color: var(--muted); }}
    .progress {{ height: 2px; background: #1a1a1a; margin-bottom: 16px; border-radius: 1px; }}
    .progress-fill {{ height: 100%; background: #2563eb; width: 0%; transition: width .3s; border-radius: 1px; }}
    .banner {{ padding: 10px 14px; border-radius: 6px; margin-bottom: 16px;
               border: 1px solid var(--border); background: var(--panel); }}
    .banner-info {{ border-color: var(--accent-border); background: var(--accent-bg); color: #d6e6ff; }}
    .banner-err {{ border-color: #7f1d1d; background: #2a0f0f; color: #fda4af; }}
    .case {{ border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px;
             margin-bottom: 8px; background: var(--panel); }}
    .case.pass {{ border-color: #1a4a1a; }}
    .case.fail {{ border-color: #4a1a1a; }}
    .case-head {{ display: flex; gap: 8px; align-items: baseline; margin-bottom: 6px; }}
    .case-n {{ color: var(--muted); font-size: 11px; min-width: 24px; }}
    .case-q {{ font-weight: 500; flex: 1; }}
    .badge {{ font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 3px; }}
    .badge-pass {{ background: #14532d; color: #86efac; }}
    .badge-fail {{ background: #7f1d1d; color: #fca5a5; }}
    .badge-running {{ background: #1e3a5f; color: #93c5fd; }}
    .case-row {{ display: flex; gap: 8px; padding: 2px 0 2px 32px; font-size: 12px; color: var(--muted); }}
    .label {{ min-width: 64px; color: #555; text-transform: uppercase; font-size: 10px; letter-spacing: .4px; padding-top: 2px; }}
    .case-response {{ padding: 6px 14px 6px 32px; font-size: 12px; color: #ccc; white-space: pre-wrap; word-break: break-word; line-height: 1.6; border-left: 2px solid #222; margin: 4px 14px 4px 32px; padding-left: 10px; }}
    .case-reason {{ padding: 2px 32px 6px; font-size: 11px; color: #888; font-style: italic; }}
    .run-btn {{ background: transparent; border: none; color: var(--accent); cursor: pointer;
                font-size: 13px; padding: 0 4px; opacity: .7; }}
    .run-btn:hover {{ opacity: 1; }}
    .add-section {{ margin-top: 24px; border-top: 1px solid var(--border); padding-top: 16px; }}
    .add-section h2 {{ font-size: 13px; font-weight: 600; margin: 0 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; }}
    textarea, input {{ width: 100%; background: #161616; border: 1px solid #2a2a2a; color: var(--text);
                        border-radius: 5px; padding: 8px 10px; font-size: 12px; font-family: inherit;
                        resize: none; outline: none; margin-bottom: 6px; }}
    textarea:focus, input:focus {{ border-color: #444; }}
    .btn-add {{ background: transparent; color: var(--muted); border: 1px solid #333;
                border-radius: 5px; padding: 5px 12px; font-size: 11px; cursor: pointer; }}
    .btn-add:hover {{ color: var(--text); border-color: #555; }}
  </style>
</head>
<body>
<div class="container">
  <h1>Eval</h1>
  <div class="meta">{case_count} case{'' if case_count == 1 else 's'} &middot; <code>{file_label}</code></div>
  {empty_banner}
  <div class="toolbar">
    <button class="btn btn-run" id="run-all">&#9654; Run All</button>
    <button class="btn btn-reset" id="reset">&#8635; Reset</button>
    <span id="status"></span>
  </div>
  <div class="progress"><div class="progress-fill" id="progress-fill"></div></div>
  <div id="cases"></div>

  <div class="add-section">
    <h2>Add case</h2>
    <textarea id="add-q" rows="2" placeholder="Test question…"></textarea>
    <input id="add-criterion" placeholder="Judge criterion (optional) — e.g. &quot;response should mention the word hello&quot;" />
    <button class="btn-add" id="add-btn">+ Add</button>
  </div>
</div>
<script>
let rows = {cases_json};

function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }}

function render() {{
  const el = document.getElementById('cases');
  if (!rows.length) {{ el.innerHTML = ''; return; }}
  el.innerHTML = rows.map((r, i) => {{
    const cls = r.status === 'pass' ? 'pass' : r.status === 'fail' ? 'fail' : '';
    const badge = r.status === 'running'
      ? '<span class="badge badge-running">running…</span>'
      : r.status === 'pass' ? '<span class="badge badge-pass">PASS</span>'
      : r.status === 'fail' ? '<span class="badge badge-fail">FAIL</span>'
      : '';
    const runBtn = r.status !== 'running'
      ? `<button class="run-btn" onclick="runCase(${{i}})" title="Run">&#9654;</button>` : '';
    const criterion = r.expected_judge || r.criterion || '';
    const expected = r.expected || '';
    return `<div class="case ${{cls}}">
      <div class="case-head">
        <span class="case-n">#${{i+1}}</span>
        <span class="case-q">${{esc(r.question || '')}}</span>
        ${{badge}} ${{runBtn}}
      </div>
      ${{criterion ? `<div class="case-row"><span class="label">Criterion</span><span>${{esc(criterion)}}</span></div>` : ''}}
      ${{expected ? `<div class="case-row"><span class="label">Expected</span><span>${{esc(expected)}}</span></div>` : ''}}
      ${{r.response ? `<div class="case-response">${{esc(r.response)}}</div>` : ''}}
      ${{r.judge_reason ? `<div class="case-reason">${{esc(r.judge_reason)}}</div>` : ''}}
    </div>`;
  }}).join('');
}}

async function runCase(i) {{
  const r = rows[i];
  r.status = 'running'; r.response = ''; r.judge_verdict = null; r.judge_reason = null;
  render();
  let text = '';
  try {{
    const resp = await fetch('/responses', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{input: [{{role: 'user', content: r.question}}], stream: true}}),
    }});
    if (!resp.ok) throw new Error(`${{resp.status}} ${{await resp.text()}}`);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {{
      const {{done, value}} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {{stream: true}});
      const lines = buf.split('\\n'); buf = lines.pop();
      for (const line of lines) {{
        if (!line.startsWith('data: ')) continue;
        try {{
          const d = JSON.parse(line.slice(6));
          if (d.type === 'response.output_text.delta' && d.delta) {{ text += d.delta; }}
          else if (d.type === 'response.output_item.done') {{
            const item = d.item || {{}};
            if (item.type === 'message' && Array.isArray(item.content)) {{
              for (const p of item.content) {{ if (p.type === 'output_text' && p.text) text += p.text; }}
            }}
          }} else if (d.type === 'response.completed' && !text) {{
            const out = d.response && d.response.output;
            if (Array.isArray(out)) for (const it of out) if (it.type === 'message' && Array.isArray(it.content))
              for (const p of it.content) if (p.type === 'output_text' && p.text) text += p.text;
          }}
        }} catch {{}}
      }}
    }}
    r.response = text || '(no response)';
    const criterion = r.expected_judge || r.criterion || '';
    if (criterion) {{
      const j = await fetch('/_apx/eval/judge', {{
        method: 'POST', headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{question: r.question, response: text, criterion}}),
      }});
      const jd = await j.json();
      r.status = jd.ok && jd.pass ? 'pass' : 'fail';
      r.judge_verdict = jd.verdict || 'ERROR';
      r.judge_reason = jd.reason || jd.error || '';
    }} else {{
      r.status = text.length > 10 ? 'pass' : 'fail';
    }}
  }} catch(e) {{
    r.response = 'Error: ' + e.message; r.status = 'fail';
  }}
  render();
  save();
}}

async function save() {{
  await fetch('/_apx/eval/data', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(rows),
  }});
}}

document.getElementById('run-all').addEventListener('click', async () => {{
  const btn = document.getElementById('run-all');
  const fill = document.getElementById('progress-fill');
  const st = document.getElementById('status');
  btn.disabled = true;
  for (let i = 0; i < rows.length; i++) {{
    st.textContent = `${{i+1}}/${{rows.length}}`;
    fill.style.width = (i / rows.length * 100) + '%';
    await runCase(i);
  }}
  fill.style.width = '100%';
  const passed = rows.filter(r => r.status === 'pass').length;
  st.textContent = `${{passed}}/${{rows.length}} passed`;
  btn.disabled = false;
}});

document.getElementById('reset').addEventListener('click', () => {{
  rows.forEach(r => {{ r.status = 'pending'; r.response = ''; r.judge_verdict = null; r.judge_reason = null; }});
  document.getElementById('progress-fill').style.width = '0%';
  document.getElementById('status').textContent = '';
  render(); save();
}});

document.getElementById('add-btn').addEventListener('click', async () => {{
  const q = document.getElementById('add-q').value.trim();
  const criterion = document.getElementById('add-criterion').value.trim();
  if (!q) return;
  rows.push({{question: q, expected_judge: criterion, status: 'pending', response: ''}});
  document.getElementById('add-q').value = '';
  document.getElementById('add-criterion').value = '';
  render(); await save();
  document.querySelector('.meta').textContent = rows.length + ' case' + (rows.length === 1 ? '' : 's');
}});

render();
</script>
</body>
</html>
"""


def _render_landing(ctx: AgentContext) -> str:
    """Server-rendered empty-chat landing: greeting + capability cards + starter chips.

    Cards come from the agent's tools (click to expand params); chips come from
    ``ctx.config.examples`` (click fills the input). Each block renders only when
    its data is present; the greeting always renders.
    """
    import html as _html
    import json as _json

    name = ctx.config.name
    desc = ctx.config.description or ""
    tools = [t for t in ctx.tools if t.name != "create_tool"]
    examples = ctx.config.examples or []

    parts = [f'<div class="landing-hi">{_html.escape(name)}</div>']
    if desc:
        parts.append(f'<div class="landing-sub">{_html.escape(desc)}</div>')

    if tools:
        cards = "".join(
            '<div class="cap-card" onclick="this.classList.toggle(&quot;open&quot;)">'
            f'<div class="cap-name">{_html.escape(t.name)}</div>'
            f'<div class="cap-desc">{_html.escape(t.description or "")}</div>'
            f'<pre class="cap-params">{_html.escape(_json.dumps(t.input_schema or {"type": "object", "properties": {}}, indent=2))}</pre>'
            '</div>'
            for t in tools
        )
        parts.append('<div class="landing-label">What I can do</div>'
                     f'<div class="cap-cards">{cards}</div>')

    if examples:
        chips = "".join(
            f'<button type="button" class="starter-chip" onclick="useExample(this)" '
            f'data-q="{_html.escape(q, quote=True)}">{_html.escape(q)} →</button>'
            for q in examples
        )
        parts.append('<div class="landing-label">Try asking</div>'
                     f'<div class="starter-chips">{chips}</div>')

    return f'<div id="landing">{"".join(parts)}</div>'


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
                '<a href="/_apx/setup" target="_top" style="color:#ffd080;text-decoration:underline">Open Setup</a> '
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
  /* Trace tab uses flex column so trace-body can scroll independently */
  #tab-trace.active {{ display: flex; flex-direction: column; height: 100%; }}

  /* Trace tab — live span bubbles, mirrors /_apx/traces/{{id}} detail view */
  .span-step {{ position: relative; padding-left: 22px; margin-bottom: 4px; }}
  .span-step .step-line {{ position: absolute; left: 6px; top: 18px; bottom: -4px; width: 1px; background: #1e1e30; }}
  .span-step:last-child .step-line {{ display: none; }}
  .span-step .step-dot {{ position: absolute; left: 1px; top: 5px; width: 11px; height: 11px; border-radius: 50%; }}
  .span-step.in-progress .step-dot {{ animation: span-pulse 1.2s ease-in-out infinite; }}
  @keyframes span-pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
  .span-step .step-content {{ padding-bottom: 10px; }}
  .span-step .step-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; font-size: 12px; }}
  .span-step .who {{ font-weight: 600; }}
  .span-step .dur {{ font-size: 10px; color: #555; font-family: monospace; }}
  .span-step .span-bubble {{ padding: 6px 10px; border-radius: 8px; font-size: 12px; line-height: 1.5;
                              white-space: pre-wrap; word-break: break-word; max-width: 100%; }}
  .span-step .span-bubble + .span-bubble {{ margin-top: 4px; }}
  .span-bubble.caller {{ background: #1a1a30; color: #b0b0c8; border: 1px solid #252545; }}
  .span-bubble.agent-ask {{ background: #0a1a25; color: #80cbc4; border: 1px solid #1a3040; }}
  .span-bubble.llm-reply {{ background: #12222e; color: #e0f0f0; border: 1px solid #1a3545; }}
  .span-bubble.tool-in {{ background: #1a1800; color: #d4c87a; border: 1px solid #2a2500; }}
  .span-bubble.tool-out {{ background: #1a1a08; color: #e0d8a0; border: 1px solid #2a2810; }}
  .span-bubble.agent-reply {{ background: #1a0a25; color: #d1a0e8; border: 1px solid #2a1a40; }}
  .span-bubble.response {{ background: #0a1a0a; color: #a0d8a0; border: 1px solid #1a3020; }}
  .span-bubble.error-msg {{ background: #1a0a0a; color: #f08080; border: 1px solid #3a1a1a; }}

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

  /* --- Landing (empty-chat) --- */
  #landing {{ padding: 28px 22px; max-width: 680px; }}
  .landing-hi {{ font-size: 19px; font-weight: 600; color: #fff; margin-bottom: 4px; }}
  .landing-sub {{ font-size: 13px; color: #8a929b; margin-bottom: 18px; line-height: 1.4; }}
  .landing-label {{ font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: #5d646c; margin: 16px 0 8px; }}
  .cap-cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .cap-card {{ background: #111418; border: 1px solid #262b31; border-radius: 8px; padding: 11px 13px; cursor: pointer; }}
  .cap-card:hover {{ border-color: #3a424b; }}
  .cap-name {{ color: #9ecbff; font-size: 12.5px; font-family: ui-monospace, monospace; }}
  .cap-desc {{ color: #9aa3ad; font-size: 11px; margin-top: 3px; line-height: 1.35; }}
  .cap-params {{ display: none; margin-top: 8px; padding-top: 8px; border-top: 1px solid #222;
                 color: #8a929b; font-size: 10.5px; white-space: pre-wrap; }}
  .cap-card.open .cap-params {{ display: block; }}
  .cap-card.open {{ border-color: #2f6b46; }}
  .starter-chip {{ display: inline-block; background: #15171a; border: 1px solid #2f343a; color: #bfe9cf;
                   border-radius: 16px; padding: 7px 13px; font-size: 12px; margin: 0 6px 7px 0; cursor: pointer; }}
  .starter-chip:hover {{ border-color: #2f6b46; }}
</style>
</head>
<body>
<header>
  <span class="badge">APX dev</span>
  <h1>{agent_name}</h1>
  <span class="desc">{agent_desc}</span>
  <nav>{_apx_nav_links("agent")}</nav>
  <button id="btn-deploy">Deploy ▶</button>
</header>
{setup_banner}
<div class="main">
  <!-- Chat (left) -->
  <div class="chat-panel">
    <div id="chat">
      {_render_landing(ctx) if ctx else f'<div class="msg system">Chat with <strong>{agent_name}</strong></div>'}
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
      <button onclick="switchTab('tools',this)">Tools</button>
      <button class="active" onclick="switchTab('trace',this)">Trace</button>
      <button onclick="switchTab('events',this)">Events</button>
      <button onclick="switchTab('eval',this)">Eval</button>
    </div>
    <div class="panel-content">
      <div id="tab-tools" class="tab-panel"></div>
      <div id="tab-trace" class="tab-panel active">
        <div id="trace-header" style="padding:8px 12px;border-bottom:1px solid #1a1a1a;font-size:11px;color:#666;display:flex;justify-content:space-between;align-items:center">
          <span id="trace-status">No trace yet — send a message</span>
          <a id="trace-link" href="#" target="_blank" style="display:none;color:#60b0ff;text-decoration:none;font-size:11px">open full →</a>
        </div>
        <div id="trace-body" style="overflow-y:auto;flex:1;padding:12px"></div>
      </div>
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
function useExample(btn) {{
  const inp = document.getElementById('input');
  inp.value = btn.dataset.q;
  inp.focus();
}}
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

function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }}

function renderEval() {{
  const el = document.getElementById('eval-cases');
  if (!evalRows.length) {{ el.innerHTML = '<div style="color:#444;font-size:12px;padding:20px 12px">No test cases. Add one below.</div>'; return; }}
  el.innerHTML = evalRows.map((r, i) => {{
    const dot = r.status === 'pass' ? '#4ade80' : r.status === 'fail' ? '#f87171' : r.status === 'running' ? '#facc15' : '#333';
    const anim = r.status === 'running' ? 'animation:pulse .8s infinite' : '';
    const ms = r.last_run_ms ? `<span style="font-size:10px;color:#555;font-family:monospace">${{r.last_run_ms}}ms</span>` : '';
    const traceLink = r.trace_id
      ? `<a href="/_apx/traces/${{esc(r.trace_id)}}" target="_blank" style="font-size:10px;color:#60b0ff;text-decoration:none">→ trace</a>`
      : '';
    const judgeBadge = r.judge_verdict
      ? `<span title="${{esc(r.judge_reason || '')}}" style="font-size:10px;font-weight:600;padding:1px 6px;border-radius:3px;`
        + (r.judge_verdict === 'PASS'
          ? `background:#052e16;color:#4ade80`
          : `background:#2a0a0a;color:#f87171`)
        + `">judge: ${{esc(r.judge_verdict)}}</span>`
      : '';
    return `<div style="padding:8px 12px;border-bottom:1px solid #141414" data-idx="${{i}}">
      <div style="display:flex;align-items:flex-start;gap:8px">
        <span style="width:8px;height:8px;border-radius:50%;background:${{dot}};${{anim}};flex-shrink:0;margin-top:4px;display:inline-block"></span>
        <span style="font-size:12px;color:#ccc;flex:1;cursor:pointer" onclick="toggleEvalResp(this)">${{esc(r.question)}}</span>
        ${{judgeBadge}} ${{ms}} ${{traceLink}}
        <button onclick="runEvalCase(${{i}})" title="Run this case" style="background:transparent;color:#60b0ff;border:none;cursor:pointer;padding:0 4px;font-size:13px" ${{r.status === 'running' ? 'disabled' : ''}}>▶</button>
        <button onclick="deleteEvalCase(${{i}})" title="Delete" style="background:transparent;color:#555;border:none;cursor:pointer;padding:0 4px;font-size:13px">✕</button>
      </div>
      <div style="margin:4px 0 0 16px">
        <input type="text" value="${{esc(r.expected || '')}}" placeholder="expected keywords (comma-separated)"
          onblur="updateExpected(${{i}}, this.value)"
          style="width:100%;background:transparent;border:none;border-bottom:1px solid #1a1a1a;color:#888;font-size:11px;padding:2px 0;outline:none" />
        <input type="text" value="${{esc(r.expected_judge || '')}}" placeholder="LLM judge criterion (e.g. 'answer mentions a temperature in fahrenheit') — overrides keywords"
          onblur="updateExpectedJudge(${{i}}, this.value)"
          style="width:100%;background:transparent;border:none;border-bottom:1px solid #1a1a1a;color:#888;font-size:11px;padding:2px 0;outline:none;margin-top:2px" />
      </div>
      ${{r.response ? `<div style="font-size:11px;color:#aaa;margin:6px 0 2px 16px;white-space:pre-wrap;line-height:1.5;border-left:2px solid #222;padding-left:8px" class="eval-resp">${{esc(r.response.slice(0,600))}}${{r.response.length>600?'…':''}}</div>` : ''}}
      ${{r.judge_reason ? `<div style="font-size:11px;color:#666;margin:2px 0 0 16px;font-style:italic">judge: ${{esc(r.judge_reason)}}</div>` : ''}}
    </div>`;
  }}).join('');
}}

function toggleEvalResp(el) {{
  // Question span → div(case) → response is the last child .eval-resp
  const wrapper = el.closest('[data-idx]');
  const resp = wrapper && wrapper.querySelector('.eval-resp');
  if (resp) resp.style.display = resp.style.display === 'none' ? '' : 'none';
}}

async function loadEvalCases() {{
  evalLoaded = true;
  try {{
    const r = await fetch('/_apx/eval/data');
    evalRows = await r.json();
    if (!Array.isArray(evalRows)) evalRows = [];
    renderEval();
  }} catch(e) {{
    document.getElementById('eval-cases').innerHTML = '<div style="color:#f87171;font-size:12px;padding:12px">Failed to load: ' + e.message + '</div>';
  }}
}}

let _evalSaveTimer = null;
function saveEvalCases() {{
  // Debounce so per-keystroke edits don't hammer the disk.
  clearTimeout(_evalSaveTimer);
  _evalSaveTimer = setTimeout(async () => {{
    try {{
      await fetch('/_apx/eval/data', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify(evalRows),
      }});
    }} catch {{ /* offline; in-memory state still works */ }}
  }}, 250);
}}

function updateExpected(i, value) {{
  if (!evalRows[i]) return;
  evalRows[i].expected = value;
  saveEvalCases();
}}

function updateExpectedJudge(i, value) {{
  if (!evalRows[i]) return;
  evalRows[i].expected_judge = value;
  saveEvalCases();
}}

function deleteEvalCase(i) {{
  evalRows.splice(i, 1);
  renderEval();
  saveEvalCases();
}}

async function runEvalCase(i) {{
  const r = evalRows[i];
  r.status = 'running'; r.response = ''; r.trace_id = null; r.last_run_ms = null;
  renderEval();
  resetTrace();

  const t0 = performance.now();
  let text = '';
  let traceId = null;
  try {{
    const resp = await fetch('/responses', {{
      method: 'POST', headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{input: [{{role:'user', content:r.question}}], stream: true}}),
    }});
    if (!resp.ok) throw new Error(`${{resp.status}} ${{await resp.text()}}`);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {{
      const {{done, value}} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {{stream:true}});
      const lines = buf.split('\\n');
      buf = lines.pop();
      for (const line of lines) {{
        if (!line.startsWith('data: ')) continue;
        try {{
          const payload = JSON.parse(line.slice(6));
          if (payload.trace_id && !traceId) traceId = payload.trace_id;
          if (payload.type === 'response.output_text.delta' && payload.delta) {{ text += payload.delta; }}
          else if (payload.type === 'response.output_item.done') {{
            const item = payload.item || {{}};
            if (item.type === 'message' && Array.isArray(item.content))
              for (const p of item.content) if (p.type === 'output_text' && p.text) text += p.text;
          }} else if (payload.type === 'response.completed' && !text) {{
            const out = payload.response && payload.response.output;
            if (Array.isArray(out)) for (const it of out) if (it.type === 'message' && Array.isArray(it.content))
              for (const p of it.content) if (p.type === 'output_text' && p.text) text += p.text;
          }}
        }} catch {{}}
      }}
    }}
    r.response = text;
    // Judge takes precedence when set; otherwise keywords; otherwise length heuristic.
    if ((r.expected_judge || '').trim()) {{
      try {{
        const j = await fetch('/_apx/eval/judge', {{
          method: 'POST', headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify({{ question: r.question, response: text, criterion: r.expected_judge }}),
        }});
        const jdata = await j.json();
        if (jdata.ok) {{
          r.judge_verdict = jdata.verdict;
          r.judge_reason = jdata.reason;
          r.status = jdata.pass ? 'pass' : 'fail';
        }} else {{
          r.judge_verdict = 'ERROR';
          r.judge_reason = jdata.error || 'Judge call failed';
          r.status = 'fail';
        }}
      }} catch (jerr) {{
        r.judge_verdict = 'ERROR';
        r.judge_reason = jerr.message;
        r.status = 'fail';
      }}
    }} else {{
      r.judge_verdict = null;
      r.judge_reason = null;
      r.status = r.expected
        ? r.expected.split(/[,;]/).map(k=>k.trim().toLowerCase()).filter(Boolean).every(k=>text.toLowerCase().includes(k)) ? 'pass' : 'fail'
        : text.length > 10 ? 'pass' : 'fail';
    }}
  }} catch(e) {{ r.response = 'Error: ' + e.message; r.status = 'fail'; }}
  r.trace_id = traceId;
  r.last_run_ms = Math.round(performance.now() - t0);
  finalizeTrace(traceId, r.status === 'fail' ? 'error' : 'completed');
  renderEval();
  saveEvalCases();
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
  evalRows.forEach(r => {{
    r.status = 'pending'; r.response = '';
    r.trace_id = null; r.last_run_ms = null;
    r.judge_verdict = null; r.judge_reason = null;
  }});
  document.getElementById('eval-progress-fill').style.width = '0%';
  document.getElementById('eval-status').textContent = '';
  renderEval();
  saveEvalCases();
}});

document.getElementById('eval-add-btn').addEventListener('click', () => {{
  const q = document.getElementById('eval-add-q').value.trim();
  if (!q) return;
  evalRows.push({{question: q, expected: '', status: 'pending', response: ''}});
  document.getElementById('eval-add-q').value = '';
  renderEval();
  saveEvalCases();
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
  // Auto-switch to events tab (3rd button: Tools, Trace, Events, Eval)
  switchTab('events', document.querySelectorAll('.panel-tabs button')[2]);
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

// ── Trace tab: live span bubbles ──
const traceBody = document.getElementById('trace-body');
const traceStatusEl = document.getElementById('trace-status');
const traceLinkEl = document.getElementById('trace-link');

function escHtml(s) {{
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

function extractMsg(value) {{
  if (value == null) return '';
  if (typeof value === 'string') {{
    try {{ return extractMsg(JSON.parse(value)); }} catch {{ return value.slice(0, 400); }}
  }}
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) {{
    for (let i = value.length - 1; i >= 0; i--) {{
      const m = value[i];
      if (m && typeof m === 'object' && 'content' in m) return extractMsg(m.content);
    }}
    return value.map(extractMsg).filter(Boolean).join(', ').slice(0, 300);
  }}
  if (typeof value === 'object') {{
    for (const k of ['content', 'text', 'output_text', 'message']) {{
      if (k in value) return extractMsg(value[k]);
    }}
    return Object.entries(value).filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => typeof v === 'string'
        ? (v.length > 60 ? `${{k}}: ${{v.slice(0, 60)}}…` : `${{k}}: ${{v}}`)
        : `${{k}}: ${{typeof v === 'number' ? v : JSON.stringify(v).slice(0, 40)}}`)
      .join('\\n');
  }}
  return String(value).slice(0, 300);
}}


function resetTrace() {{
  traceBody.innerHTML = '<div style="color:#555;font-size:12px;padding:8px 0">Running…</div>';
  traceStatusEl.textContent = 'running…';
  traceLinkEl.style.display = 'none';
}}

async function finalizeTrace(traceId, status, opts) {{
  opts = opts || {{}};
  traceStatusEl.textContent = status === 'error' ? 'errored' : 'done';
  if (!traceId) {{
    // ResponsesAgent doesn't emit trace_id in the stream — fall back to the
    // most recent trace logged in this experiment.
    try {{
      await new Promise(r => setTimeout(r, 500)); // let MLflow flush the trace
      const r = await fetch('/_apx/traces?fmt=json&max=1');
      const rows = await r.json();
      if (rows && rows.length) traceId = rows[0].trace_id;
    }} catch {{}}
  }}
  if (!traceId) {{
    traceBody.innerHTML = '<div style="color:#555;font-size:12px;padding:8px 0">No trace found.</div>';
    return;
  }}
  traceLinkEl.href = `/_apx/traces/${{traceId}}`;
  traceLinkEl.style.display = 'inline';
  // Load and render spans inline. ``mlflow.langchain.autolog()`` writes its
  // spans as artifacts on a background thread, so the first fetch can race
  // and return ``[]`` even when the trace exists. Retry once after 1.5s before
  // declaring "no spans".
  try {{
    let data;
    for (let attempt = 0; attempt < 2; attempt++) {{
      const r = await fetch(`/_apx/traces/${{traceId}}?fmt=json`);
      data = await r.json();
      if (data.spans && data.spans.length) break;
      if (data.error) break;
      await new Promise(res => setTimeout(res, 1500));
    }}
    if (data.error || !data.spans || !data.spans.length) {{
      traceBody.innerHTML = `<div style="color:#555;font-size:12px;padding:8px 0">${{data.error || 'No spans.'}}</div>`;
      return;
    }}
    traceBody.innerHTML = '';
    // Build parent→children
    const byParent = {{}};
    const roots = [];
    for (const s of data.spans) {{
      if (s.parent_id) (byParent[s.parent_id] = byParent[s.parent_id] || []).push(s);
      else roots.push(s);
    }}
    const SPAN_COLORS = {{LLM:'#22d3ee',TOOL:'#facc15',CHAIN:'#a78bfa',AGENT:'#60b0ff',OTHER:'#94a3b8'}};
    function spanTypeShort(t) {{
      t = (t||'').toUpperCase();
      if (['LLM','CHAT_MODEL','EMBEDDING'].includes(t)) return 'LLM';
      if (['TOOL','RETRIEVER'].includes(t)) return 'TOOL';
      if (t === 'CHAIN') return 'CHAIN';
      if (t === 'AGENT') return 'AGENT';
      return 'OTHER';
    }}
    function renderSpanNode(s, depth) {{
      const type = spanTypeShort(s.span_type);
      const color = SPAN_COLORS[type] || '#888';
      const dur = s.duration_ms != null ? `${{s.duration_ms}}ms` : '';
      const isErr = (s.status||'').toUpperCase().includes('ERR');
      const wrap = document.createElement('div');
      wrap.style.cssText = `padding-left:${{depth*14}}px;margin-bottom:3px;`;
      const card = document.createElement('div');
      card.className = 'span-step';
      card.style.cssText = 'position:relative;padding-left:18px;';
      const dot = document.createElement('div');
      dot.className = 'step-dot';
      dot.style.cssText = `position:absolute;left:1px;top:5px;width:9px;height:9px;border-radius:50%;background:${{color}};`;
      const content = document.createElement('div');
      content.className = 'step-content';
      const header = document.createElement('div');
      header.className = 'step-header';
      header.innerHTML =
        `<span class="who" style="color:${{color}};font-size:12px;font-weight:600">${{escHtml(s.name)}}</span>` +
        `<span style="font-size:10px;color:#555;font-family:monospace;margin-left:4px">${{type}}</span>` +
        (dur ? `<span class="dur" style="margin-left:auto">${{dur}}</span>` : '') +
        (isErr ? `<span style="color:#f87171;font-size:10px;margin-left:8px">ERR</span>` : '');
      content.appendChild(header);
      // Show inputs/outputs compactly
      for (const [label, val] of [['in', s.inputs], ['out', s.outputs]]) {{
        if (!val) continue;
        const msg = extractMsg(val);
        if (!msg) continue;
        const bubble = document.createElement('div');
        const bubbleCls = label === 'in' ? (type === 'TOOL' ? 'tool-in' : 'agent-ask') : (type === 'TOOL' ? 'tool-out' : 'llm-reply');
        bubble.className = `span-bubble ${{bubbleCls}}`;
        bubble.style.cssText = 'font-size:11px;margin-top:3px;';
        bubble.textContent = msg.slice(0, 300) + (msg.length > 300 ? '…' : '');
        content.appendChild(bubble);
      }}
      card.appendChild(dot); card.appendChild(content);
      wrap.appendChild(card);
      for (const child of (byParent[s.span_id] || [])) {{
        wrap.appendChild(renderSpanNode(child, depth + 1));
      }}
      return wrap;
    }}
    for (const root of roots) traceBody.appendChild(renderSpanNode(root, 0));

    // Surface tool calls + results in the Events panel so it stops only
    // showing the user/assistant "one side" of the conversation. Gated on
    // ``opts.emitEvents`` so the page-load autoload (which fetches the most
    // recent historical trace) doesn't pollute the events list before the
    // user has even sent a message.
    if (opts.emitEvents) {{
      // Spans come back roughly in start order; preserve that for the
      // events list so call/result interleave correctly across multi-tool runs.
      const orderedSpans = [...data.spans].sort(
        (a, b) => (a.start_time_ns || 0) - (b.start_time_ns || 0)
      );
      for (const s of orderedSpans) {{
        if ((s.span_type || '').toUpperCase() !== 'TOOL') continue;
        const args = s.inputs || {{}};
        const isErr = (s.status || '').toUpperCase().includes('ERR');
        const result = s.outputs;
        const dur = s.duration_ms != null ? `${{s.duration_ms}}ms` : '';
        addEvent('tool-call', s.name, fmt(args).slice(0, 60), {{ arguments: args }});
        addEvent(
          isErr ? 'tool-error' : 'tool-result',
          s.name,
          dur,
          {{ result: result }}
        );
      }}
    }}
  }} catch(e) {{
    traceBody.innerHTML = `<div style="color:#f87171;font-size:12px">${{escHtml(e.message)}}</div>`;
  }}
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
  document.getElementById('landing')?.remove();
  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;

  addMsg('user', text);
  addEvent('user', text.slice(0, 80), null, {{ content: text }});
  history.push({{ role: 'user', content: text }});
  resetTrace();

  const assistantDiv = addMsg('assistant', '', true);
  let full = '';
  let pendingTrace = null;
  let traceId = null;
  let traceStatus = 'completed';

  try {{
    const res = await fetch('/responses', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'x-return-trace-id': 'true' }},
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
      for (const line of lines) {{
        if (!line.startsWith('data: ')) continue;
        try {{
          const payload = JSON.parse(line.slice(6));
          const ptype = payload.type || '';
          if (payload.trace_id && !traceId) traceId = payload.trace_id;
          if (ptype === 'response.output_text.delta' && payload.delta) {{
            full += payload.delta;
            assistantDiv.textContent = full;
            chat.scrollTop = chat.scrollHeight;
          }} else if (ptype === 'response.output_item.done') {{
            const item = payload.item || {{}};
            if (item.type === 'message' && Array.isArray(item.content)) {{
              for (const part of item.content) {{
                if (part.type === 'output_text' && part.text) {{
                  full += part.text;
                  assistantDiv.textContent = full;
                  chat.scrollTop = chat.scrollHeight;
                }}
              }}
            }}
          }} else if (ptype === 'response.completed' && !full) {{
            const out = payload.response && payload.response.output;
            if (Array.isArray(out)) {{
              for (const item of out) {{
                if (item.type === 'message' && Array.isArray(item.content)) {{
                  for (const part of item.content) {{
                    if (part.type === 'output_text' && part.text) full += part.text;
                  }}
                }}
              }}
              if (full) assistantDiv.textContent = full;
            }}
          }} else if (ptype === 'tool.trace') {{
            // Intentional dormant hook: no current producer emits ``tool.trace``.
            // Kept for a future server-side path that streams per-tool events so
            // inline pills can render mid-conversation. Today the Events panel
            // covers the same need by harvesting TOOL spans from the trace
            // after the response completes (see ``finalizeTrace``). Don't
            // delete in audit-chain sweeps.
            if (Array.isArray(payload.tools) && payload.tools.length) addToolPills(payload.tools);
          }} else if (ptype === 'error') {{
            traceStatus = 'error';
          }}
        }} catch {{}}
      }}
    }}
  }} catch (err) {{
    full = `Error: ${{err.message}}`;
    assistantDiv.textContent = full;
    traceStatus = 'error';
  }}

  assistantDiv.classList.remove('streaming');
  addEvent('assistant', full.slice(0, 80) + (full.length > 80 ? '…' : ''), null, {{ content: full }});
  history.push({{ role: 'assistant', content: full }});
  finalizeTrace(traceId, traceStatus, {{ emitEvents: true }});
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

// Auto-populate the Trace panel on page load with the most recent run, so
// `reload → click Trace` shows context instead of an empty pane.
finalizeTrace(null, 'done');

inputEl.focus();
</script>
{_deploy_overlay_html()}
</body>
</html>"""


def _build_apx_openapi_spec(
    ctx: AgentContext | None,
    api_prefix: str = "/api",
    base_url: str | None = None,
) -> dict[str, Any]:
    """Build an OpenAPI 3.1 spec containing only tool endpoints with dep-stripped schemas.

    This is what the LLM sees — not the full FastAPI route signatures (which include
    injected deps like WorkspaceClient). Used by /_apx/openapi.json and Scalar.

    When ``base_url`` is provided, the spec includes a ``servers`` field pointing at
    that origin. Without it, Scalar falls back to ``http://localhost`` for "Try it"
    URLs and curl examples — wrong on every deployed app. The route handler in
    ``_dev.py`` passes ``request.base_url`` so the spec self-describes its host.
    """
    servers = [{"url": base_url.rstrip("/")}] if base_url else []
    if ctx is None:
        return {
            "openapi": "3.1.0",
            "info": {"title": "Agent Tools", "version": "0.0.0"},
            **({"servers": servers} if servers else {}),
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
        **({"servers": servers} if servers else {}),
        "paths": paths,
    }
