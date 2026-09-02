"""Dev UI — /_apx/agent unified shell, chat interface, OpenAPI spec builder, and /_apx/tools inspector."""

from __future__ import annotations

import os
from typing import Any

from ._models import AgentContext, AgentTool, workflow_prompts, workflows_for_context
from ._ui_nav import _apx_nav_links, _deploy_overlay_html


# Tabs exposed by the unified shell at /_apx/agent. Each entry is
# (slug, label, iframe URL). The shell defaults to the first tab. To
# add a tab, append here — the shell auto-renders it and the URL
# fragment-router handles selection.
_UNSET_ENV = ""  # optional UI env vars (catalog, warehouse) resolve to empty when not set

_UNIFIED_TABS: tuple[tuple[str, str, str], ...] = (
    ("chat", "Chat", "/_apx/chat"),
    ("edit", "Edit", "/_apx/edit"),
    ("eval", "Eval", "/_apx/eval"),
    # "setup" is intentionally not a shell tab — its data-source + tool
    # generation flow is reached from the Edit page's "✨ From data" modal.
    ("discover", "Discover", "/_apx/discover"),
    ("grounding", "Grounding", "/_apx/grounding"),
    ("knowledge", "Knowledge", "/_apx/knowledge"),
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
    html, body {{ margin: 0; height: 100%; display: flex; flex-direction: column;
                  background: var(--bg); color: var(--text);
                  font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; font-size: 13px; }}
    header {{ height: 52px; padding: 0 16px; display: flex; align-items: center; flex-shrink: 0;
              gap: 12px; background: var(--panel); border-bottom: 1px solid var(--border); }}
    .badge {{ background: var(--accent-bg); color: var(--accent); font-size: 11px;
              font-weight: 600; padding: 3px 8px; border-radius: 4px; letter-spacing: .5px;
              text-transform: uppercase; }}
    .title {{ display: flex; flex-direction: column; gap: 1px; }}
    .agent-name {{ font-weight: 600; font-size: 14px; }}
    .agent-desc {{ color: var(--text-muted); font-size: 11px; max-width: 360px;
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
    /* Native <details> dropdown for standard agent endpoints — no JS. */
    .util-menu {{ position: relative; }}
    .util-menu > summary {{ list-style: none; }}
    .util-menu > summary::-webkit-details-marker {{ display: none; }}
    .util-menu-panel {{ position: absolute; right: 0; top: calc(100% + 6px); z-index: 60;
                        min-width: 210px; background: var(--panel);
                        border: 1px solid var(--border-strong); border-radius: 6px;
                        padding: 4px; box-shadow: 0 8px 24px rgba(0,0,0,.45); }}
    .util-menu-panel a {{ display: flex; align-items: center; gap: 10px; padding: 7px 9px;
                          color: var(--text-muted); text-decoration: none;
                          border-radius: 4px; font-size: 12px; white-space: nowrap; }}
    .util-menu-panel a:hover {{ background: var(--accent-bg); color: var(--text); }}
    .util-menu-panel a .path {{ margin-left: auto; color: #555; font-size: 10px;
                                font-family: ui-monospace, monospace; }}
    #btn-sidebar-toggle {{ background: none; border: none; color: #555; font-size: 16px;
                           cursor: pointer; padding: 4px 8px; border-radius: 5px; line-height: 1;
                           flex-shrink: 0; }}
    #btn-sidebar-toggle:hover {{ color: #aaa; background: #1a1a1a; }}
    /* Layout: sidebar + content */
    .shell-body {{ flex: 1; display: flex; min-height: 0; }}
    #sidebar {{ width: 220px; flex-shrink: 0; background: #0b0b0b;
                border-right: 1px solid #1a1a1a; display: flex; flex-direction: column;
                overflow: hidden; transition: width .18s ease; }}
    body.sidebar-off #sidebar {{ width: 0; }}
    #sidebar-scroll {{ flex: 1; overflow-y: auto; padding: 8px 0 16px; }}
    /* Sidebar identity block */
    .sb-identity {{ padding: 10px 14px 8px; border-bottom: 1px solid #141414; margin-bottom: 6px; }}
    .sb-host {{ font-family: ui-monospace,monospace; font-size: 10px; color: #60b0ff;
                text-decoration: none; display: block; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }}
    .sb-host:hover {{ text-decoration: underline; }}
    .sb-user {{ font-size: 10px; color: #444; margin-top: 2px; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }}
    /* Section label */
    .sb-section {{ font-size: 10px; color: #3a3a3a; text-transform: uppercase;
                   letter-spacing: .08em; padding: 10px 14px 4px; }}
    /* Nav items */
    .sb-item {{ display: flex; align-items: center; gap: 10px; padding: 7px 14px;
                cursor: pointer; border-radius: 0; user-select: none;
                color: #666; font-size: 13px; border: none; background: none;
                width: 100%; text-align: left; text-decoration: none; }}
    .sb-item:hover {{ background: #141414; color: #bbb; }}
    .sb-item.active {{ background: #0d1f38; color: var(--accent); }}
    .sb-icon {{ width: 18px; flex-shrink: 0; text-align: center; font-size: 14px;
                line-height: 1; opacity: .75; }}
    .sb-item:hover .sb-icon, .sb-item.active .sb-icon {{ opacity: 1; }}
    .sb-label {{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    /* Data tree inside sidebar */
    .sb-tree {{ padding: 0 6px; }}
    .tree-row {{ display: flex; align-items: center; gap: 7px; padding: 6px 8px;
                 border-radius: 5px; cursor: pointer; user-select: none; }}
    .tree-row:hover {{ background: #141414; color: #bbb; }}
    .tree-chevron {{ flex-shrink: 0; width: 10px; color: #333; transition: transform .15s;
                     font-size: 9px; line-height: 1; }}
    .tree-row.open > .tree-chevron {{ transform: rotate(90deg); color: #555; }}
    .tree-icon {{ flex-shrink: 0; font-size: 13px; line-height: 1; width: 16px;
                  text-align: center; opacity: .8; }}
    .tree-name {{ font-family: ui-monospace, monospace; font-size: 11px; flex: 1;
                  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .tree-cat > .tree-name {{ color: #c9d1d9; font-weight: 500; }}
    .tree-schemas {{ padding-left: 22px; position: relative; }}
    .tree-schemas::before {{ content: ''; position: absolute; left: 13px; top: 0;
                              bottom: 4px; width: 1px; background: #1e1e1e; }}
    .tree-sch > .tree-name {{ color: #8b949e; }}
    .tree-tables {{ padding-left: 22px; position: relative; }}
    .tree-tables::before {{ content: ''; position: absolute; left: 13px; top: 0;
                             bottom: 4px; width: 1px; background: #191919; }}
    .tree-tbl {{ display: flex; align-items: center; gap: 7px; padding: 5px 8px;
                 border-radius: 4px; cursor: pointer; user-select: none; }}
    .tree-tbl:hover {{ background: #0f1f14; }}
    .tree-tbl:hover > .tree-name {{ color: #4ade80; }}
    .tree-tbl > .tree-name {{ color: #3d4a40; font-size: 10px; transition: color .1s; }}
    .tree-loading {{ padding: 4px 8px; color: #2a2a2a; font-size: 11px; font-style: italic; }}
    main {{ flex: 1; min-width: 0; background: var(--bg); }}
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
    <button id="btn-sidebar-toggle" title="Toggle sidebar">☰</button>
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
    <details class="util-menu">
      <summary class="util-btn" title="Standard agent endpoints">
        <span class="ico">◇</span> Endpoints
      </summary>
      <div class="util-menu-panel">
        <a href="/.well-known/agent.json" target="_blank">A2A card <span class="path">/.well-known/agent.json</span></a>
        <a href="/_apx/openapi.json" target="_blank">API spec <span class="path">/_apx/openapi.json</span></a>
        <a href="/health" target="_blank">Health <span class="path">/health</span></a>
        <a href="/readyz" target="_blank">Readiness <span class="path">/readyz</span></a>
      </div>
    </details>
  </header>
  <div class="shell-body">
    <aside id="sidebar">
      <div id="sidebar-scroll">
        <div id="sb-identity" class="sb-identity">
          <div class="sb-user" style="color:#333">Loading…</div>
        </div>
        <div class="sb-section">Data</div>
        <div class="sb-tree"><div id="cat-tree"></div></div>
      </div>
    </aside>
    <main><iframe id="dash-frame" src="{default_src}"></iframe></main>
  </div>
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
      window._selectTab = selectTab;
      try {{
        const q = new URLSearchParams(location.search);
        if (q.get("wired") === "1") {{
          const toast = document.createElement("div");
          toast.textContent = "Discover wiring applied — try it in Chat. A full redeploy is only needed if hot-apply failed.";
          toast.style.cssText = "position:fixed;bottom:20px;left:50%;transform:translateX(-50%);z-index:3000;background:#052e1c;border:1px solid #14532d;color:#4ade80;padding:10px 16px;border-radius:8px;font-size:12px;max-width:90vw;";
          document.body.appendChild(toast);
          setTimeout(() => toast.remove(), 8000);
          q.delete("wired");
          const next = location.pathname + (q.toString() ? "?" + q : "") + location.hash;
          history.replaceState(null, "", next);
        }}
      }} catch (e) {{}}
      window._dashFrame = frame;
      // Sidebar toggle
      document.getElementById("btn-sidebar-toggle").addEventListener("click", () => {{
        document.body.classList.toggle("sidebar-off");
      }});
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
    // ── Sidebar data ──
    function selectTable(fqn) {{
      // Stay on Eval if it's active (it handles apx:table-selected directly).
      // For all other tabs, navigate to Chat first.
      const activeSlug = (location.hash || "#{default_slug}").slice(1);
      if (activeSlug !== "eval" && window._selectTab) window._selectTab("chat");
      const frame = window._dashFrame || document.getElementById("dash-frame");
      setTimeout(() => {{
        frame.contentWindow.postMessage({{type: "apx:table-selected", fqn}}, "*");
      }}, 150);
    }}
    async function loadSidebar() {{
      try {{
        const d = await fetch("/_apx/workspace-context").then(r => r.json());
        const id = document.getElementById("sb-identity");
        if (id) {{
          id.innerHTML = `
            <a class="sb-host" href="${{d.host||'#'}}" target="_blank">${{(d.host||'').replace('https://','')}}</a>
            <div class="sb-user">${{d.user||''}}</div>`;
        }}
        loadCatalogTree(d.used_catalogs||[], d.used_schemas||[]);
      }} catch(e) {{
        const id = document.getElementById("sb-identity");
        if (id) id.innerHTML = '<div class="sb-user">Unavailable</div>';
      }}
    }}
    loadSidebar();
    function loadCatalogTree(usedCats, usedSchemas) {{
      const tree = document.getElementById("cat-tree");
      if (!tree) return;
      if (!usedCats.length) {{
        tree.innerHTML = '<div class="tree-loading">No UC resources declared</div>';
        return;
      }}
      // Group schemas by catalog
      const bycat = {{}};
      for (const cs of usedSchemas) {{
        const [cat, sch] = cs.split(".");
        if (!bycat[cat]) bycat[cat] = [];
        bycat[cat].push(sch);
      }}
      tree.innerHTML = usedCats.map(c => {{
        const schemas = (bycat[c] || []).map(s => `<div>
          <div class="tree-row tree-sch" onclick="toggleSchema(this,'${{c}}','${{s}}')">
            <span class="tree-chevron">›</span>
            <span class="tree-icon" style="font-size:10px">◫</span>
            <span class="tree-name">${{s}}</span>
          </div>
          <div class="tree-tables" style="display:none"></div>
        </div>`).join("");
        return `<div>
          <div class="tree-row tree-cat open" onclick="this.classList.toggle('open');this.nextElementSibling.style.display=this.classList.contains('open')?'block':'none'">
            <span class="tree-chevron">›</span>
            <span class="tree-icon">🗄</span>
            <span class="tree-name">${{c}}</span>
          </div>
          <div class="tree-schemas">${{schemas}}</div>
        </div>`;
      }}).join("");
    }}
    async function toggleSchema(rowEl, catalog, schema) {{
      rowEl.classList.toggle("open");
      const tablesEl = rowEl.nextElementSibling;
      if (tablesEl.style.display === "none") {{
        tablesEl.style.display = "block";
        if (!tablesEl.dataset.loaded) {{
          tablesEl.dataset.loaded = "1";
          tablesEl.innerHTML = '<div class="tree-loading">Loading…</div>';
          try {{
            const tables = await fetch(`/_apx/setup/tables?catalog=${{catalog}}&schema=${{schema}}`).then(r => r.json());
            tablesEl.innerHTML = tables.length
              ? tables.map(t => `<div class="tree-tbl" onclick="selectTable('${{catalog}}.${{schema}}.${{t}}')" title="Ask about ${{t}}"><span class="tree-icon" style="font-size:9px;color:#333">▦</span><span class="tree-name">${{t}}</span></div>`).join("")
              : '<div class="tree-loading">no tables</div>';
          }} catch(e) {{ tablesEl.innerHTML = '<div class="tree-loading">Error</div>'; }}
        }}
      }} else {{
        tablesEl.style.display = "none";
      }}
    }}
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

<div class="container" style="margin-top:0;padding-top:0">
  <div class="add-section">
    <h2>Judge Alignment</h2>
    <p style="color:var(--muted);font-size:12px;margin:0 0 12px">
      Rate traces with 👍/👎 in the <a href="/_apx/traces" style="color:var(--accent)">Traces</a> view,
      then create a labeling session and run MemAlign to align your judge.
    </p>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <input id="la-judge" placeholder="Judge name (e.g. quality)" style="max-width:220px;margin:0" />
      <button class="btn btn-run" id="la-start-btn" onclick="labelStart()">Start session</button>
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <input id="la-run-id" placeholder="Run ID (from start session)" style="max-width:320px;margin:0" />
      <button class="btn btn-run" id="la-align-btn" onclick="labelAlign()">Run alignment</button>
    </div>
    <div id="la-status" style="font-size:12px;color:var(--muted);min-height:18px"></div>
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
  // Surface a rejected save (422 bad shape / 503 no agent_router / 500 write
  // error) instead of swallowing it — a silent save reads as success while
  // nothing persisted. A network error (offline) stays silent: in-memory
  // state still works and the next save retries.
  try {{
    const resp = await fetch('/_apx/eval/data', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(rows),
    }});
    if (!resp.ok) {{
      let msg = resp.status + ' ' + resp.statusText;
      try {{ const d = await resp.json(); msg = d.error || (d.detail && JSON.stringify(d.detail)) || msg; }} catch {{}}
      const st = document.getElementById('status');
      if (st) st.textContent = 'Save failed: ' + msg;
      console.error('eval save failed:', msg);
    }}
  }} catch (e) {{
    console.error('eval save error (offline?):', e);
  }}
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

// Handle table selection from the parent shell's sidebar.
window.addEventListener('message', (e) => {{
  if (e.data?.type !== 'apx:table-selected') return;
  const fqn = e.data.fqn || '';
  const table = fqn.split('.').pop();
  const suggestions = [
    `How many rows are in ${{fqn}}?`,
    `What are the most recent records in ${{table}}?`,
    `Show me a sample of data from ${{table}}`,
    `What are the column names and types in ${{table}}?`,
  ];
  // Pre-seed suggested eval cases for the selected table.
  let added = 0;
  for (const q of suggestions) {{
    if (!rows.some(r => r.question === q)) {{
      rows.push({{question: q, expected_judge: '', status: 'pending', response: ''}});
      added++;
    }}
  }}
  if (added) {{
    render(); save();
    document.querySelector('.meta').textContent = rows.length + ' case' + (rows.length === 1 ? '' : 's');
  }}
  // Scroll to + highlight the add-question box for any manual additions.
  const addQ = document.getElementById('add-q');
  if (addQ) {{
    addQ.value = `Ask about ${{table}}: `;
    addQ.focus();
    document.querySelector('.add-section').scrollIntoView({{behavior: 'smooth'}});
  }}
}}}});

// ── Judge Alignment ──────────────────────────────────────────────────────────
async function labelStart() {{
  const judge = document.getElementById('la-judge').value.trim();
  if (!judge) {{ document.getElementById('la-status').textContent = 'Enter a judge name.'; return; }}
  document.getElementById('la-start-btn').disabled = true;
  document.getElementById('la-status').textContent = 'Starting…';
  try {{
    const r = await fetch('/_apx/eval/label-start', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{judge_name: judge}}),
    }});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    document.getElementById('la-run-id').value = d.run_id;
    document.getElementById('la-status').innerHTML =
      `Session created (${{d.trace_count}} traces). ` +
      (d.session_url ? `<a href="${{d.session_url}}" target="_blank">Open Review App →</a>` : `run_id: ${{d.run_id}}`);
  }} catch(e) {{
    document.getElementById('la-status').textContent = 'Error: ' + e.message;
  }} finally {{
    document.getElementById('la-start-btn').disabled = false;
  }}
}}

async function labelAlign() {{
  const judge = document.getElementById('la-judge').value.trim();
  const run_id = document.getElementById('la-run-id').value.trim();
  if (!judge || !run_id) {{ document.getElementById('la-status').textContent = 'Judge name and run ID required.'; return; }}
  document.getElementById('la-align-btn').disabled = true;
  document.getElementById('la-status').textContent = 'Aligning… (this may take a minute)';
  try {{
    const r = await fetch('/_apx/eval/label-align', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{judge_name: judge, run_id}}),
    }});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    const gs = (d.guidelines || []).map((g, i) => `${{i+1}}. ${{g}}`).join('<br>');
    document.getElementById('la-status').innerHTML =
      `✓ Aligned as <strong>${{d.registered_as}}</strong><br><small>${{gs}}</small>`;
  }} catch(e) {{
    document.getElementById('la-status').textContent = 'Error: ' + e.message;
  }} finally {{
    document.getElementById('la-align-btn').disabled = false;
  }}
}}
</script>
</body>
</html>
"""


def _render_landing(ctx: AgentContext) -> str:
    """Server-rendered empty-chat landing: greeting + capability cards + starter chips.

    Cards come from the agent's tools (click to expand params); chips come from
    configured examples and workflows (click fills the input). Each block renders
    only when its data is present; the greeting always renders.
    """
    import html as _html
    import json as _json

    name = ctx.config.name
    desc = ctx.config.description or ""
    tools = [t for t in ctx.tools if t.name != "create_tool"]
    resolved_workflows = workflows_for_context(ctx)
    examples = workflow_prompts(ctx.config, resolved_workflows)
    workflows = {workflow.question: workflow for workflow in resolved_workflows}

    parts = [f'<div class="landing-hi">{_html.escape(name)}</div>']
    if desc:
        parts.append(f'<div class="landing-sub">{_html.escape(desc)}</div>')

    schema = getattr(ctx, "schema", None)
    if schema and isinstance(schema.get("tables"), dict) and schema["tables"]:
        schema_name = schema.get("schema", "") or schema.get("catalog", "")
        tbls = schema["tables"]
        shown_tbls = list(tbls.items())[:12]
        pills = "".join(
            f'<span class="data-pill">{_html.escape(tname)}</span>'
            for tname, _ in shown_tbls
        )
        more = f'<span class="data-pill data-pill-more">+{len(tbls) - 12} more</span>' if len(tbls) > 12 else ""
        n = len(tbls)
        parts.append(
            '<div class="data-card">'
            f'<div class="data-card-head">{_html.escape(schema_name)} &mdash; '
            f'{n} table{"s" if n != 1 else ""}</div>'
            f'<div class="data-pills">{pills}{more}</div>'
            '</div>'
        )

    if tools:
        _MEM_OPS = {"recall", "remember", "forget"}

        def _is_mem_tool(name: str) -> bool:
            n = name.lower()
            return any(n == op or n.endswith("_" + op) for op in _MEM_OPS)

        mem_tools = [t for t in tools if _is_mem_tool(t.name)]
        other_tools = [t for t in tools if not _is_mem_tool(t.name)]

        def _tool_card(t: AgentTool) -> str:
            return (
                '<div class="cap-card" onclick="this.classList.toggle(&quot;open&quot;)">'
                f'<div class="cap-name">{_html.escape(t.name)}</div>'
                f'<div class="cap-desc">{_html.escape(t.description or "")}</div>'
                f'<pre class="cap-params">{_html.escape(_json.dumps(t.input_schema or {"type": "object", "properties": {}}, indent=2))}</pre>'
                '</div>'
            )

        cards = "".join(_tool_card(t) for t in other_tools)

        if mem_tools:
            mem_table = getattr(getattr(ctx.config, "memory", None), "table_name", None) or ""
            mem_link = (
                f' <a href="#" class="cap-mem-link" data-mem-table="{_html.escape(mem_table)}"'
                f' title="{_html.escape(mem_table)}">↗ table</a>'
                if mem_table else ""
            )
            cards += (
                '<div class="cap-card">'
                f'<div class="cap-name">🧠 memory{mem_link}</div>'
                '<div id="cap-mem-preview" class="cap-mem-preview"></div>'
                '</div>'
            )

        parts.append('<div class="landing-label">What I can do</div>'
                     f'<div class="cap-cards">{cards}</div>')

    if examples:
        def _render_example(q: str) -> str:
            workflow = workflows.get(q)
            if workflow is None:
                return (
                    f'<button type="button" class="starter-chip" onclick="useExample(this)" '
                    f'data-q="{_html.escape(q, quote=True)}">{_html.escape(q)}</button>'
                )

            # The browser decodes character references in data attributes, so
            # use one for '?' to avoid serializing the question twice while
            # preserving the exact value consumed by useExample().
            data_q = _html.escape(q, quote=True).replace("?", "&#x3f;")
            return (
                f'<button type="button" class="starter-chip workflow-chip" onclick="useExample(this)" '
                f'data-q="{data_q}">'
                f'<span class="workflow-title">{_html.escape(workflow.title)}</span>'
                f'<span class="workflow-purpose">{_html.escape(workflow.purpose)}</span>'
                f'<span class="workflow-question">{_html.escape(q)}</span>'
                '</button>'
            )

        chips = "".join(_render_example(q) for q in examples)
        parts.append('<div class="landing-label">Try asking</div>'
                     f'<div class="starter-chips">{chips}</div>')

    return f'<div id="landing">{"".join(parts)}</div>'


def _render_agent_ui(ctx: AgentContext | None, *, embed: bool = False) -> str:
    """Return a self-contained HTML page for interactively testing the agent."""
    import json as _json

    agent_name = ctx.config.name if ctx else "Agent"
    agent_desc = ctx.config.description if ctx else ""
    body_class = ' class="apx-embed"' if embed else ""
    trace_active = ' class="active"' if embed else ""
    events_active = "" if embed else ' class="active"'
    trace_panel_class = "tab-panel active" if embed else "tab-panel"
    events_panel_class = "tab-panel" if embed else "tab-panel active"
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
    # First-run wizard nudge: show banner if no catalog/warehouse configured.
    # Read from disk (.env) so changes without restart are reflected.
    if not not_configured and ctx:
        from pathlib import Path
        _dotenv: dict[str, str] = {}
        for _dotenv_path in (Path.cwd() / ".env", Path.cwd() / ".env.local"):
            try:
                for _line in _dotenv_path.read_text().splitlines():
                    if "=" in _line and not _line.lstrip().startswith("#"):
                        _k, _, _v = _line.partition("=")
                        _dotenv[_k.strip()] = _v.strip().strip('"').strip("'")
            except Exception:
                pass
        _env_catalog = (
            _dotenv.get("DEMO_CATALOG") or _dotenv.get("CATALOG")
            or os.environ.get("DEMO_CATALOG") or os.environ.get("CATALOG", _UNSET_ENV)
        )
        _env_wh = _dotenv.get("WAREHOUSE_ID") or os.environ.get("WAREHOUSE_ID", _UNSET_ENV)
        # Suppress banner when the agent already has tools — DataAgent/CoworkerAgent
        # pre-ground their schema in code, so DEMO_CATALOG/WAREHOUSE_ID never get set.
        _has_tools = ctx and any(t.name != "create_tool" for t in ctx.tools)
        if (not _env_catalog or not _env_wh) and not _has_tools:
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
  /* --- Rendered assistant markdown (tables, code, lists, headings) --- */
  .msg.assistant table {{ border-collapse: collapse; margin: 8px 0; font-size: 12px; width: 100%; }}
  .msg.assistant th, .msg.assistant td {{ border: 1px solid #2a2a2a; padding: 5px 9px; text-align: left; }}
  .msg.assistant th {{ background: #161616; color: #cfe; font-weight: 600; }}
  .msg.assistant tr:nth-child(even) td {{ background: #0f0f0f; }}
  .msg.assistant pre {{ background: #111; border: 1px solid #222; border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 12px; }}
  .msg.assistant code {{ background: #15171a; border-radius: 4px; padding: 1px 5px; font-size: 12px; font-family: ui-monospace, monospace; }}
  .msg.assistant pre code {{ background: none; padding: 0; }}
  .msg.assistant h1, .msg.assistant h2, .msg.assistant h3 {{ margin: 10px 0 6px; line-height: 1.3; }}
  .msg.assistant ul, .msg.assistant ol {{ margin: 6px 0 6px 20px; }}
  .msg.assistant li {{ margin: 2px 0; }}
  .msg.assistant a {{ color: #60b0ff; }}
  .msg.assistant p {{ margin: 6px 0; }}
  .msg.assistant > :first-child {{ margin-top: 0; }}
  .msg.assistant > :last-child {{ margin-bottom: 0; }}
  .trace-inline {{ align-self: flex-start; display: flex; align-items: center; gap: 8px;
                   margin: -8px 0 4px; font-size: 11px; color: #6b7280; }}
  .trace-inline button, .trace-inline a {{ background: transparent; border: 1px solid #1e3a5f;
                                           border-radius: 5px; color: #60b0ff; cursor: pointer;
                                           font: inherit; padding: 3px 8px; text-decoration: none; }}
  .trace-inline button:hover, .trace-inline a:hover {{ background: #0d1f38; }}
  .trace-inline code {{ color: #93c5fd; font-family: ui-monospace, monospace; }}
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

  /* Inline thinking-steps (live tool rows above the answer) */
  .inline-steps {{ align-self: flex-start; width: 100%; }}
  .inline-step {{ background: #0e1116; border: 1px solid #1f242b; border-radius: 8px; margin: 6px 0; padding: 0; max-width: 680px; }}
  .inline-step.error {{ border-color: #3a1a1a; }}
  .inline-step-head {{ display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; font-size: 12.5px; }}
  .inline-step-head .step-icon {{ color: #60b0ff; }}
  .inline-step.error .step-icon {{ color: #f87171; }}
  .inline-step-head .step-name {{ color: #cfe; font-family: ui-monospace, monospace; }}
  .inline-step-head .step-label {{ color: #6b7280; margin-left: auto; font-size: 11px; }}
  .inline-step-head .step-caret {{ color: #4b5563; font-size: 10px; transition: transform .12s; }}
  .inline-step.open .step-caret {{ transform: rotate(90deg); }}
  .inline-step-detail {{ display: none; margin: 0; padding: 0 12px 10px; }}
  .inline-step.open .inline-step-detail {{ display: block; }}
  .step-detail-label {{ font-size: 10px; color: #5b6470; text-transform: uppercase; letter-spacing: .5px; margin: 8px 0 3px; }}
  .step-detail-pre {{ margin: 0; color: #8a929b; font-size: 11px; white-space: pre-wrap;
                      font-family: ui-monospace, monospace; }}
  .step-sql {{ display: block; color: #8a929b; font-size: 11px; white-space: pre-wrap;
               font-family: ui-monospace, monospace; }}
  .resp-table-wrap {{ overflow-x: auto; max-width: 100%; }}
  .resp-table {{ border-collapse: collapse; font-size: 11px; width: 100%; margin-bottom: 4px; }}
  .resp-table th {{ color: #60b0ff; text-align: left; padding: 3px 8px;
                    border-bottom: 1px solid #2a2a2a; font-weight: 600; white-space: nowrap; }}
  .resp-table td {{ color: #c9d1d9; padding: 3px 8px; border-bottom: 1px solid #1a1a1a; }}
  .resp-table tr:last-child td {{ border-bottom: none; }}
  .resp-meta {{ font-size: 10px; color: #4b5563; margin-top: 4px; }}
  .trunc-note {{ color: #f59e0b; }}

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
  #tab-history.active {{ display: flex; flex-direction: column; height: 100%; }}
  .conv-toolbar {{ padding: 8px 12px; border-bottom: 1px solid #1a1a1a; display: flex; align-items: center; justify-content: flex-end; flex-shrink: 0; }}
  .conv-new-btn {{ background: transparent; color: #60b0ff; border: 1px solid #1e3a5f; border-radius: 5px; padding: 4px 10px; font-size: 12px; cursor: pointer; }}
  .conv-new-btn:hover {{ background: #0d1f38; }}
  .conv-list {{ overflow-y: auto; flex: 1; }}
  .conv-item {{ padding: 10px 12px; cursor: pointer; border-bottom: 1px solid #111; transition: background .1s; }}
  .conv-item:hover {{ background: #131313; }}
  .conv-item.active {{ background: #0d1f38; border-left: 2px solid #60b0ff; padding-left: 10px; }}
  .conv-title {{ font-size: 13px; color: #ccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .conv-item.active .conv-title {{ color: #60b0ff; }}
  .conv-ts {{ font-size: 11px; color: #444; margin-top: 2px; }}

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
  /* Tool-call group: call + response paired in one collapsible block. */
  .event.tool-group {{ flex-direction: column; align-items: stretch; gap: 0; padding: 0; cursor: default; }}
  .event.tool-group:hover {{ background: transparent; }}
  .tg-head {{ display: flex; align-items: center; gap: 10px; padding: 10px 16px; cursor: pointer; }}
  .tg-head:hover {{ background: #151515; }}
  .tg-name {{ flex: 1; color: #60b0ff; font-family: ui-monospace, monospace; font-size: 13px;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .tg-hint {{ color: #4b5563; font-size: 11px; white-space: nowrap; }}
  .tg-caret {{ color: #555; font-size: 10px; flex-shrink: 0; transition: transform .12s; }}
  .event.tool-group:not(.open) .tg-caret {{ transform: rotate(-90deg); }}
  .tg-body {{ display: none; flex-direction: column; gap: 1px; padding: 0 16px 8px 52px; }}
  .event.tool-group.open .tg-body {{ display: flex; }}
  .tg-part {{ display: flex; gap: 10px; font-size: 12px; line-height: 1.5; cursor: pointer;
              padding: 3px 6px; border-radius: 4px; }}
  .tg-part:hover {{ background: #141414; }}
  .tg-label {{ flex: none; min-width: 62px; color: #6b7686; text-transform: uppercase;
               font-size: 10px; letter-spacing: .4px; font-weight: 600; padding-top: 1px; }}
  .tg-part.err .tg-label {{ color: #f87171; }}
  .tg-val {{ color: #cbd2da; font-family: ui-monospace, monospace; min-width: 0;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

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
  .data-card {{ background: #0e1116; border: 1px solid #1f242b; border-radius: 10px;
                padding: 12px 14px; margin: 10px 0; max-width: 680px; }}
  .data-card-head {{ font-size: 12.5px; color: #9aa3ad; margin-bottom: 8px; }}
  .data-pills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .data-pill {{ background: #151a20; border: 1px solid #252d35; border-radius: 5px;
                padding: 3px 9px; font-size: 11.5px; color: #9ecbff;
                font-family: ui-monospace, monospace; white-space: nowrap; }}
  .data-pill-more {{ color: #6b7280; border-color: #1f242b; }}
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
  .cap-mem-link {{ color: #60b0ff; font-size: 10px; text-decoration: none; margin-left: 6px; vertical-align: middle; opacity: .7; }}
  .cap-mem-link:hover {{ opacity: 1; }}
  .cap-mem-preview {{ margin-top: 8px; display: flex; flex-direction: column; gap: 3px; }}
  .cap-mem-preview:empty {{ display: none; }}
  .cap-mem-row {{ font-size: 11.5px; color: #9ecbff; line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .cap-mem-row-ts {{ font-size: 10px; color: #667; font-family: ui-monospace, monospace; margin-left: 4px; }}
  .cap-mem-ops {{ margin-top: 8px; padding-top: 8px; border-top: 1px solid #222; display: flex; flex-direction: column; gap: 4px; }}
  .cap-mem-op {{ display: flex; flex-direction: column; gap: 1px; }}
  .cap-mem-name {{ color: #9ecbff; font-size: 11px; font-family: ui-monospace, monospace; }}
  .cap-mem-desc {{ color: #8a929b; font-size: 10.5px; line-height: 1.35; }}
  .starter-chip {{ display: inline-block; background: #15171a; border: 1px solid #2f343a; color: #bfe9cf;
                   border-radius: 8px; padding: 9px 16px; font-size: 13px; margin: 0 8px 8px 0;
                   cursor: pointer; text-align: left; transition: background 0.12s, border-color 0.12s; }}
  .starter-chip:hover {{ background: #1c2822; border-color: #2f6b46; }}
  .starter-chip:active {{ background: #213326; }}
  .workflow-chip {{ display: inline-flex; flex-direction: column; gap: 3px; min-width: 220px; }}
  .workflow-title {{ color: #d8f3df; font-weight: 600; }}
  .workflow-purpose {{ color: #8a929b; font-size: 11px; }}
  .workflow-question {{ color: #bfe9cf; font-size: 12px; }}

  /* Compact embed mode for topology's right rail. Same chat implementation,
     without the standalone page chrome or side-by-side desktop split. */
  body.apx-embed header,
  body.apx-embed #setup-banner,
  body.apx-embed .mcp-bar,
  body.apx-embed .resize-handle {{
    display: none !important;
  }}
  body.apx-embed .main {{
    flex-direction: column;
    height: 100vh;
  }}
  body.apx-embed .chat-panel {{
    min-height: 0;
  }}
  body.apx-embed #chat {{
    padding: 16px 18px;
    gap: 12px;
  }}
  body.apx-embed #landing {{
    display: none;
  }}
  body.apx-embed .msg,
  body.apx-embed .inline-step,
  body.apx-embed .data-card {{
    max-width: 100%;
  }}
  body.apx-embed .right-panel {{
    width: 100%;
    min-width: 0;
    max-width: none;
    flex: 0 0 42%;
    min-height: 210px;
    border-left: 0;
    border-top: 1px solid #1a1a1a;
  }}
  body.apx-embed .panel-tabs button {{
    font-size: 12px;
    padding: 9px 0;
  }}
  body.apx-embed .panel-content {{
    min-height: 0;
  }}
  body.apx-embed .cap-cards {{
    grid-template-columns: 1fr;
  }}
  body.apx-embed .input-bar {{
    padding: 12px 14px;
  }}
  body.apx-embed .input-bar textarea {{
    font-size: 14px;
    padding: 10px 12px;
  }}
</style>
</head>
<body{body_class}>
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
      <button onclick="switchTab('history',this)">History</button>
      <button onclick="switchTab('tools',this)">Tools</button>
      <button{trace_active} onclick="switchTab('trace',this)">Trace</button>
      <button{events_active} onclick="switchTab('events',this)">Events</button>
      <button onclick="switchTab('eval',this)">Eval</button>
    </div>
    <div class="panel-content">
      <div id="tab-history" class="tab-panel">
        <div class="conv-toolbar">
          <button class="conv-new-btn" onclick="newConversation()">+ New</button>
        </div>
        <div id="conv-list" class="conv-list">
          <div class="empty-state">No conversations yet</div>
        </div>
      </div>
      <div id="tab-tools" class="tab-panel"></div>
      <div id="tab-trace" class="{trace_panel_class}">
        <div id="trace-header" style="padding:8px 12px;border-bottom:1px solid #1a1a1a;font-size:11px;color:#666;display:flex;justify-content:space-between;align-items:center">
          <span id="trace-status">No trace yet — send a message</span>
          <a id="trace-link" href="#" target="_blank" style="display:none;color:#60b0ff;text-decoration:none;font-size:11px">open full →</a>
        </div>
        <div id="trace-body" style="overflow-y:auto;flex:1;padding:12px"></div>
      </div>
      <div id="tab-events" class="{events_panel_class}">
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

<!-- Vendored locally (no CDN) so the deployed app is offline/private-link safe. -->
<script src="/_apx/vendor/marked.min.js"></script>
<script src="/_apx/vendor/purify.min.js"></script>

<script>
const TOOLS = {tools_json};
function useExample(btn) {{
  const inp = document.getElementById('input');
  inp.value = btn.dataset.q;
  form.requestSubmit();
}}
// Table selected from the workspace drawer (parent frame postMessage)
window.addEventListener('message', (e) => {{
  if (e.data?.type !== 'apx:table-selected') return;
  const fqn = e.data.fqn || '';
  const table = fqn.split('.').pop();
  const questions = [
    `What columns does ${{fqn}} have?`,
    `Show me a sample of data from ${{table}}`,
    `How many rows are in ${{table}}?`,
    `What are the most recent records in ${{table}}?`,
  ];
  const chips = questions.map(q =>
    `<button type="button" class="starter-chip" onclick="useExample(this)" data-q="${{q}}">${{q}}</button>`
  ).join('');
  const chatEl = document.getElementById('chat');
  chatEl.innerHTML = `
    <div style="padding:32px 24px 12px">
      <div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">Table selected</div>
      <div style="font-family:ui-monospace,monospace;font-size:13px;color:#c9d1d9;margin-bottom:16px">${{fqn}}</div>
      <div class="starter-chips">${{chips}}</div>
    </div>`;
  document.getElementById('input').placeholder = 'Ask about ${{table}}…';
  document.getElementById('input').focus();
}});
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
    const raw = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    resultBox.innerHTML = fmtResp(raw);
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
  if (name === 'history') loadConversationHistory();
}}

// ── History tab ──
let _convHistoryTimer = null;
function loadConversationHistory() {{
  if (_convHistoryTimer) clearTimeout(_convHistoryTimer);
  _convHistoryTimer = setTimeout(_doLoadConversationHistory, 120);
}}
function _doLoadConversationHistory() {{
  _convHistoryTimer = null;
  fetch('/_apx/conversations').then(r => r.json()).then(convs => {{
    const list = document.getElementById('conv-list');
    if (!convs.length) {{
      list.innerHTML = '<div class="empty-state">No conversations yet — send a message</div>';
      return;
    }}
    list.innerHTML = convs.map(c => {{
      const label = c.title || ('Conv ' + c.id.slice(-8));
      const ts = c.updated_at ? new Date(c.updated_at).toLocaleDateString() : '';
      const active = c.id === devThreadId ? ' active' : '';
      return `<div class="conv-item${{active}}" data-id="${{c.id}}" onclick="switchConversation('${{c.id}}')">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div class="conv-title" style="flex:1">${{esc(label)}}</div>
          <button onclick="event.stopPropagation();promoteToEval('${{c.id}}',${{JSON.stringify(label)}})"
            title="Promote to Eval"
            style="background:transparent;border:1px solid #2a2a2a;border-radius:3px;color:#555;font-size:9px;padding:1px 5px;cursor:pointer;flex-shrink:0;margin-left:4px">→ Eval</button>
        </div>
        ${{ts ? `<div class="conv-ts">${{ts}}</div>` : ''}}
      </div>`;
    }}).join('');
  }}).catch(() => {{}});
}}

function newConversation() {{
  devThreadId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36);
  sessionStorage.setItem(_THREAD_KEY, devThreadId);
  chat.innerHTML = '';
  history.length = 0;  // reset request payload so a new conversation starts clean
  loadConversationHistory();
}}

// ── Approvals (ASK policy human-in-the-loop) ──
// Backend: GET /_apx/approvals, POST /_apx/approvals/{{id}}/approve|deny (see _dev.py).
// ASK policy is non-blocking/retry-based: a gated tool is refused for the turn,
// the turn ends, the card surfaces here, and approving auto-submits a retry turn.
async function checkPendingApprovals() {{
  let pending = [];
  try {{
    const r = await fetch('/_apx/approvals');
    if (!r.ok) return;
    pending = await r.json();
    if (!Array.isArray(pending)) return;
  }} catch {{ return; }}
  for (const a of pending) {{
    // One card per approval; skip if already rendered.
    if (document.querySelector(`[data-approval-id="${{a.id}}"]`)) continue;
    const card = document.createElement('div');
    card.className = 'approval-card';
    card.dataset.approvalId = a.id;
    card.style.cssText = 'margin:8px 0;padding:10px 12px;border:1px solid #6b4f00;border-radius:8px;background:#1a1400';
    const argsStr = JSON.stringify(a.arguments || {{}}, null, 0);
    card.innerHTML = `
      <div style="font-size:12px;color:#facc15;font-weight:600;margin-bottom:4px">⏸ Approval required: ${{esc(a.tool_name)}}</div>
      ${{a.reason ? `<div style="font-size:11px;color:#aaa;margin-bottom:4px">${{esc(a.reason)}}</div>` : ''}}
      <div style="font-size:11px;color:#888;font-family:monospace;margin-bottom:8px;word-break:break-all">${{esc(argsStr.slice(0, 300))}}</div>
      <button onclick="resolveApproval('${{a.id}}', true, this)" style="background:#14532d;color:#4ade80;border:1px solid #166534;border-radius:5px;padding:4px 14px;cursor:pointer;font-size:12px;margin-right:8px">Approve</button>
      <button onclick="resolveApproval('${{a.id}}', false, this)" style="background:#2a0a0a;color:#f87171;border:1px solid #7f1d1d;border-radius:5px;padding:4px 14px;cursor:pointer;font-size:12px">Deny</button>`;
    chat.appendChild(card);
    chat.scrollTop = chat.scrollHeight;
  }}
}}

async function resolveApproval(id, approved, btn) {{
  const card = btn.closest('.approval-card');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
  try {{
    const r = await fetch(`/_apx/approvals/${{id}}/${{approved ? 'approve' : 'deny'}}`, {{ method: 'POST' }});
    if (!r.ok) throw new Error('HTTP ' + r.status);
  }} catch (err) {{
    if (card) card.querySelectorAll('button').forEach(b => b.disabled = false);
    addMsg('assistant', `Approval update failed: ${{err.message}}`, false);
    return;
  }}
  if (card) card.remove();
  // Close the loop: tell the agent the decision so it retries (approved) or
  // moves on (denied) without the user retyping.
  inputEl.value = approved
    ? `I approved request ${{id}}. Please retry the action.`
    : `I denied request ${{id}}. Do not retry that action.`;
  form.requestSubmit();
}}

async function promoteToEval(convId, label) {{
  try {{
    const items = await fetch(`/_apx/conversations/${{convId}}/items`).then(r => r.json());
    // Find the first user message and the first assistant response
    const userMsg = items.find(it => it.type === 'message' && it.data && it.data.role === 'user');
    const assistantMsg = items.find(it => it.type === 'message' && it.data && it.data.role === 'assistant');
    const question = userMsg ? (Array.isArray(userMsg.data.content)
      ? userMsg.data.content.map(p => p.text || '').join('') : userMsg.data.content || '') : label;
    const response = assistantMsg ? (Array.isArray(assistantMsg.data.content)
      ? assistantMsg.data.content.map(p => p.text || '').join('') : assistantMsg.data.content || '') : '';
    if (!evalLoaded) {{ await loadEvalCases(); }}
    evalRows.push({{ question: question.trim(), expected_judge: '', response: response.trim(), status: 'pending' }});
    saveEvalCases();
    switchTab('eval', document.querySelectorAll('.panel-tabs button')[4]);
    renderEval();
  }} catch(e) {{ alert('Failed to promote: ' + e.message); }}
}}

async function switchConversation(id) {{
  devThreadId = id;
  sessionStorage.setItem(_THREAD_KEY, id);
  chat.innerHTML = '';
  history.length = 0;  // reset request payload so the switched-to conversation starts clean
  document.querySelectorAll('.conv-item').forEach(el => {{
    el.classList.toggle('active', el.dataset.id === id);
  }});
  try {{
    const items = await fetch(`/_apx/conversations/${{id}}/items`).then(r => r.json());
    for (const item of items) {{
      if (item.type !== 'message') continue;
      const role = item.data?.role;
      if (!role) continue;
      const blocks = item.data?.content || [];
      const text = blocks.map(b => b.text || b.content || '').join('');
      if (text) addMsg(role, text, false);
    }}
  }} catch {{}}
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

function _evalSaveBanner(msg) {{
  // Single reusable banner as a sibling *before* #eval-cases, so renderEval()
  // (which rewrites #eval-cases.innerHTML) never wipes it. msg === null clears.
  const host = document.getElementById('eval-cases');
  if (!host || !host.parentNode) return;
  let b = document.getElementById('eval-save-err');
  if (msg === null) {{ if (b) b.remove(); return; }}
  if (!b) {{
    b = document.createElement('div');
    b.id = 'eval-save-err';
    b.style.cssText = 'color:#f87171;font-size:12px;padding:8px 12px;background:#2a0a0a;border-bottom:1px solid #401414';
    host.parentNode.insertBefore(b, host);
  }}
  b.textContent = 'Save failed: ' + msg;
}}

let _evalSaveTimer = null;
function saveEvalCases() {{
  // Debounce so per-keystroke edits don't hammer the disk.
  clearTimeout(_evalSaveTimer);
  _evalSaveTimer = setTimeout(async () => {{
    try {{
      const resp = await fetch('/_apx/eval/data', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify(evalRows),
      }});
      // Surface a rejected save (422/503/500) instead of swallowing it; a
      // silent save reads as success while nothing persisted.
      if (!resp.ok) {{
        let msg = resp.status + ' ' + resp.statusText;
        try {{ const d = await resp.json(); msg = d.error || (d.detail && JSON.stringify(d.detail)) || msg; }} catch {{}}
        _evalSaveBanner(msg);
        console.error('eval save failed:', msg);
      }} else {{
        _evalSaveBanner(null);
      }}
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
let apxHost = '';
let apxMemoryTable = '';
(async () => {{
  try {{
    const d = await fetch('/_apx/workspace-context').then(r => r.json());
    apxHost = d.host || '';
    apxMemoryTable = d.memory_table || '';
    // Wire the ↗ table link to the actual Databricks UC explorer URL.
    if (apxHost && apxMemoryTable) {{
      const link = document.querySelector('.cap-mem-link[data-mem-table]');
      if (link) {{
        const parts = apxMemoryTable.split('.');
        if (parts.length === 3) {{
          link.href = `${{apxHost}}/explore/data/${{parts[0]}}/${{parts[1]}}/${{parts[2]}}`;
          link.target = '_blank';
          link.rel = 'noreferrer';
        }}
      }}
    }}
  }} catch {{}}
  // Populate the landing page memory preview with recent stored memories.
  try {{
    const mems = await fetch('/_apx/memories').then(r => r.json());
    const preview = document.getElementById('cap-mem-preview');
    if (preview && Array.isArray(mems) && mems.length) {{
      preview.innerHTML = mems.slice(0, 3).map(m => {{
        const ts = m.updated_at ? m.updated_at.slice(0, 10) : '';
        const tsSpan = ts ? `<span class="cap-mem-row-ts">${{ts}}</span>` : '';
        return `<div class="cap-mem-row" title="${{m.content.replace(/"/g, '&quot;')}}">${{m.content}}${{tsSpan}}</div>`;
      }}).join('');
    }}
  }} catch {{}}
}})();

// Stable session key for the dev-UI conversation.  Stored in sessionStorage so
// a page refresh resumes the same server-side session (the user doesn't lose
// agent memory / conversation state).  A new tab always gets a fresh UUID.
const _THREAD_KEY = '_apx_dev_thread_id';
let devThreadId = sessionStorage.getItem(_THREAD_KEY);
if (!devThreadId) {{
  devThreadId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36);
  sessionStorage.setItem(_THREAD_KEY, devThreadId);
}}

// Load the conversation history list on page load (History is the default tab).
loadConversationHistory();
// Surface any approvals already pending when the UI is (re)opened.
checkPendingApprovals();

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
  // Label tool rows clearly as call vs response (icons alone aren't obvious).
  // The tool name (when present and not the generic "tool") is appended.
  const kindLabel = {{ 'tool-call': 'tool call', 'tool-result': 'tool response', 'tool-error': 'tool error' }}[type] || '';
  const shownTitle = kindLabel
    ? (title && title !== 'tool' ? `${{kindLabel}} · ${{title}}` : kindLabel)
    : title;
  div.innerHTML = `<span class="event-num">#${{num}}</span><span class="event-icon">${{icons[type] || '•'}}</span>`
    + `<div class="event-body"><div class="event-title">${{shownTitle}}</div>`
    + (subtitle ? `<div class="event-sub">${{subtitle}}</div>` : '') + '</div>';
  div.onclick = () => showDetail(ev, div);
  eventsList.appendChild(div);
  eventsList.scrollTop = eventsList.scrollHeight;
  return ev;
}}

// ── Tool event grouping ──
// Each tool call + its response are grouped into one collapsible block keyed by
// call_id (request and response together), so you read "ran X → got Y" as a
// unit instead of all-calls-then-all-responses interleaved. Non-tool events
// (user/assistant) stay flat rows via addEvent. Memory tools (recall/remember/
// forget) share one "memory" card regardless of how many calls there are.
// Reset per send.
const toolGroups = {{}};     // groupId -> {{ body }}
const memCallBodies = {{}};  // call_id  -> sub-body within the shared memory card
const MEM_GROUP_ID = '__apx_memory__';

function isMemoryTool(name) {{
  if (typeof name !== 'string') return false;
  const n = name.toLowerCase();
  return ['recall','remember','forget'].some(k => n === k || n.endsWith('_' + k));
}}
function memOpLabel(name) {{
  const n = (name || '').toLowerCase();
  if (n === 'recall'   || n.endsWith('_recall'))   return 'recall';
  if (n === 'remember' || n.endsWith('_remember')) return 'remember';
  if (n === 'forget'   || n.endsWith('_forget'))   return 'forget';
  return name;
}}
function isRecallTool(name) {{
  return typeof name === 'string' && (name === 'recall' || name.endsWith('_recall'));
}}
const RECALL_TOP_N = 3;
function parseMemoryLines(text) {{
  if (!text || text.trim() === 'No memories found.') return [];
  return text.split('\\n')
    .filter(l => l.trim().startsWith('-'))
    .map(l => {{
      const m = l.match(/^\\s*-\\s*\\[score=([\\d.]+)\\]\\s*(.*)/);
      return m ? {{ score: parseFloat(m[1]), content: m[2] }}
               : {{ score: null, content: l.replace(/^\\s*-\\s*/, '').trim() }};
    }})
    .filter(item => item.content);
}}

function addToolCall(groupId, name, reqText, reqData) {{
  if (!eventsStarted) {{ eventsList.innerHTML = ''; eventsStarted = true; }}
  const num = eventCounter++;
  const reqEv = {{ num, type: 'tool-call', title: name || 'tool', subtitle: reqText, data: reqData }};
  events.push(reqEv);

  // Memory tools share one card.
  if (isMemoryTool(name)) {{
    let memGroup = toolGroups[MEM_GROUP_ID];
    if (!memGroup) {{
      const group = document.createElement('div');
      group.className = 'event tool-group open';
      const head = document.createElement('div');
      head.className = 'tg-head';
      head.innerHTML = `<span class="event-num">#${{num}}</span><span class="event-icon">🧠</span>`
        + `<span class="tg-name">memory</span><span class="tg-hint">request + response</span><span class="tg-caret">▾</span>`;
      head.onclick = () => group.classList.toggle('open');
      const body = document.createElement('div');
      body.className = 'tg-body';
      group.appendChild(head);
      group.appendChild(body);
      eventsList.appendChild(group);
      toolGroups[MEM_GROUP_ID] = {{ body }};
      memGroup = toolGroups[MEM_GROUP_ID];
    }}
    // Each call gets its own sub-container so its response lands in the right spot.
    const sub = document.createElement('div');
    sub.style.cssText = 'display: contents;';
    memGroup.body.appendChild(sub);
    const reqRow = document.createElement('div');
    reqRow.className = 'tg-part';
    reqRow.innerHTML = `<span class="tg-label">${{memOpLabel(name)}}</span>`
      + `<span class="tg-val">${{esc(reqText || '')}}</span>`;
    reqRow.onclick = () => showDetail(reqEv, null);
    sub.appendChild(reqRow);
    memCallBodies[groupId] = {{ sub, name }};
    eventsList.scrollTop = eventsList.scrollHeight;
    return reqEv;
  }}

  const group = document.createElement('div');
  group.className = 'event tool-group open';
  group.dataset.idx = events.length - 1;
  const head = document.createElement('div');
  head.className = 'tg-head';
  head.innerHTML = `<span class="event-num">#${{num}}</span><span class="event-icon">⚡</span>`
    + `<span class="tg-name">${{esc(name || 'tool')}}</span><span class="tg-hint">request + response</span><span class="tg-caret">▾</span>`;
  head.onclick = () => group.classList.toggle('open');
  const body = document.createElement('div');
  body.className = 'tg-body';
  const reqRow = document.createElement('div');
  reqRow.className = 'tg-part';
  reqRow.innerHTML = '<span class="tg-label">request</span>'
    + `<span class="tg-val">${{esc(reqText || '')}}</span>`;
  reqRow.onclick = () => showDetail(reqEv, null);
  body.appendChild(reqRow);
  group.appendChild(head);
  group.appendChild(body);
  eventsList.appendChild(group);
  eventsList.scrollTop = eventsList.scrollHeight;
  toolGroups[groupId] = {{ body, callEv: reqEv }};
  return reqEv;
}}

function _appendMemoryUcLink(container) {{
  if (!apxHost || !apxMemoryTable) return;
  const parts = apxMemoryTable.split('.');
  const ucUrl = parts.length === 3
    ? `${{apxHost}}/explore/data/${{parts[0]}}/${{parts[1]}}/${{parts[2]}}`
    : `${{apxHost}}/explore/data`;
  const a = document.createElement('a');
  a.href = ucUrl; a.target = '_blank';
  a.textContent = `${{apxMemoryTable}} ↗`;
  a.style.cssText = 'font-size:11px;color:#555;font-family:ui-monospace,monospace;text-decoration:none;';
  a.onmouseover = () => a.style.color = '#60b0ff';
  a.onmouseout  = () => a.style.color = '#555';
  a.onclick = e => e.stopPropagation();
  container.appendChild(a);
}}

function addToolResponse(groupId, name, respText, respData, isErr) {{
  // Memory tool response — route into the shared memory card's sub-container.
  if (memCallBodies[groupId]) {{
    const {{ sub, name: toolName }} = memCallBodies[groupId];
    const ev = {{ num: '', type: isErr ? 'tool-error' : 'tool-result',
                 title: toolName || 'tool', subtitle: respText, data: respData }};
    events.push(ev);
    const fullText = (respData && (respData.output || respData.result)) || respText || '';
    // Recall: show top-N items with "View all" toggle + UC link.
    if (!isErr && isRecallTool(toolName)) {{
      const items = parseMemoryLines(fullText);
      if (items.length > 0) {{
        const row = document.createElement('div');
        row.className = 'tg-part';
        row.style.cssText = 'cursor:pointer;align-items:flex-start;';
        row.onclick = () => showDetail(ev, null);
        const lbl = document.createElement('span');
        lbl.className = 'tg-label'; lbl.textContent = 'found';
        row.appendChild(lbl);
        const valDiv = document.createElement('div');
        valDiv.style.cssText = 'flex:1;min-width:0;display:flex;flex-direction:column;gap:2px;';
        const itemStyle = 'font-size:12px;color:#cbd2da;font-family:ui-monospace,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
        items.slice(0, RECALL_TOP_N).forEach(item => {{
          const el = document.createElement('div');
          el.style.cssText = itemStyle; el.title = item.content;
          el.textContent = (item.score != null ? `[${{item.score.toFixed(2)}}] ` : '') + item.content;
          valDiv.appendChild(el);
        }});
        const rest = items.slice(RECALL_TOP_N);
        if (rest.length > 0) {{
          const restDiv = document.createElement('div');
          restDiv.style.cssText = 'display:none;flex-direction:column;gap:2px;';
          rest.forEach(item => {{
            const el = document.createElement('div');
            el.style.cssText = itemStyle; el.title = item.content;
            el.textContent = (item.score != null ? `[${{item.score.toFixed(2)}}] ` : '') + item.content;
            restDiv.appendChild(el);
          }});
          valDiv.appendChild(restDiv);
          const btn = document.createElement('button');
          btn.textContent = `View all (${{items.length}}) ▸`;
          btn.style.cssText = 'background:none;border:none;color:#60b0ff;font-size:11px;cursor:pointer;padding:2px 0;text-align:left;font-family:ui-monospace,monospace;';
          btn.onclick = e => {{
            e.stopPropagation();
            const open = restDiv.style.display !== 'none';
            restDiv.style.display = open ? 'none' : '';
            btn.textContent = open ? `View all (${{items.length}}) ▸` : '▾ Collapse';
          }};
          valDiv.appendChild(btn);
        }} else {{
          const c = document.createElement('div');
          c.style.cssText = 'font-size:11px;color:#555;font-family:ui-monospace,monospace;';
          c.textContent = `${{items.length}} memor${{items.length === 1 ? 'y' : 'ies'}}`;
          valDiv.appendChild(c);
        }}
        _appendMemoryUcLink(valDiv);
        row.appendChild(valDiv);
        sub.appendChild(row);
        eventsList.scrollTop = eventsList.scrollHeight;
        return ev;
      }}
    }}
    // remember/forget: plain response row.
    const row = document.createElement('div');
    row.className = 'tg-part' + (isErr ? ' err' : '');
    row.innerHTML = `<span class="tg-label">${{isErr ? 'error' : 'saved'}}</span>`
      + `<span class="tg-val">${{esc(respText || '')}}</span>`;
    row.onclick = () => showDetail(ev, null);
    sub.appendChild(row);
    eventsList.scrollTop = eventsList.scrollHeight;
    return ev;
  }}

  const g = toolGroups[groupId];
  // Unmatched response (no preceding call captured) → fall back to a flat row.
  if (!g) return addEvent(isErr ? 'tool-error' : 'tool-result', name || 'tool', respText, respData);
  const ev = {{ num: '', type: isErr ? 'tool-error' : 'tool-result',
               title: name || 'tool', subtitle: respText, data: respData }};
  events.push(ev);
  // For SQL tools: update the request row and call event detail to show SQL.
  // respData.result may be a parsed object (trace replay) or string (live stream).
  let respDisplay = respText || '';
  if (!isErr && respData) {{
    const rawResult = respData.output || respData.result;
    if (rawResult) {{
      try {{
        const parsed = typeof rawResult === 'string' ? JSON.parse(rawResult) : rawResult;
        if (parsed._sql) {{
          // Update compact request row with SQL preview
          const reqPart = g.body.querySelector('.tg-part');
          if (reqPart) {{
            const valEl = reqPart.querySelector('.tg-val');
            if (valEl) valEl.textContent = parsed._sql.replace(/\\s+/g, ' ').trim().slice(0, 160);
          }}
          // Inject SQL into the call event so the detail panel can show it
          if (g.callEv && g.callEv.data) g.callEv.data._sql = parsed._sql;
          // Build a clean response summary: "N rows · query took Xs"
          const rowCount = Array.isArray(parsed.data) ? parsed.data.length : null;
          const timingRaw = parsed._timing || '';
          const timing = timingRaw.replace(/^\\[|\\]$/g, '');
          respDisplay = [
            rowCount != null ? `${{rowCount}} row${{rowCount !== 1 ? 's' : ''}}` : null,
            timing || null
          ].filter(Boolean).join(' · ');
        }}
      }} catch {{}}
    }}
  }}
  const row = document.createElement('div');
  row.className = 'tg-part' + (isErr ? ' err' : '');
  row.innerHTML = `<span class="tg-label">${{isErr ? 'error' : 'response'}}</span>`
    + `<span class="tg-val">${{esc(respDisplay)}}</span>`;
  row.onclick = () => showDetail(ev, null);
  g.body.appendChild(row);
  eventsList.scrollTop = eventsList.scrollHeight;
  return ev;
}}

function showDetail(ev, el) {{
  document.querySelectorAll('.event.selected').forEach(e => e.classList.remove('selected'));
  if (el) el.classList.add('selected');
  detailTitle.textContent = `#${{ev.num}} ${{ev.type}}`;
  let html = '';
  if (ev.data) {{
    if (ev.type === 'assistant' && ev.data.content) {{
      // Render assistant responses as markdown in the detail panel.
      try {{ html = DOMPurify.sanitize(marked.parse(ev.data.content)); }}
      catch {{ html = `<pre>${{esc(ev.data.content)}}</pre>`; }}
    }} else {{
      // For tool events use the same formatters as inline steps: SQL gets a
      // code block, row arrays get a table — everything else falls back to <pre>.
      const isCallKey = k => k === 'arguments' || k === 'inputs' || k === 'args';
      const isRespKey = k => k === 'result' || k === 'outputs' || k === 'output';
      // For tool-call events: show SQL as the request when available (set
      // retroactively by addToolResponse once the result arrives).
      if (ev.type === 'tool-call' && ev.data._sql) {{
        html = `<div class="label">request</div>${{fmtSql(ev.data._sql)}}`;
      }} else {{
        for (const [k, v] of Object.entries(ev.data)) {{
          if (k === '_sql') continue;  // already shown above or handled in fmtResp
          const raw = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
          const body = isCallKey(k) ? fmtReq(raw) : isRespKey(k) ? fmtResp(raw) : `<pre>${{esc(raw)}}</pre>`;
          html += `<div class="label">${{esc(k)}}</div>${{body}}`;
        }}
      }}
    }}
  }}
  detailBody.innerHTML = html;
  detailPanel.classList.add('open');
  // Auto-switch to events tab (4th button: History, Tools, Trace, Events, Eval)
  switchTab('events', document.querySelectorAll('.panel-tabs button')[3]);
}}

function closeDetail() {{
  detailPanel.classList.remove('open');
  document.querySelectorAll('.event.selected').forEach(e => e.classList.remove('selected'));
}}

// ── Chat ──
function renderAssistantInto(el, text) {{
  // Sanitized markdown → HTML for assistant messages. marked parses, DOMPurify
  // strips anything unsafe (escape-by-default; the model never injects raw HTML).
  try {{
    el.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
    // Block-level HTML now owns the layout — turn off the pre-wrap that the
    // plain-text fallback relies on, otherwise marked's inter-block newline
    // text-nodes render as visible blank lines (double-spaced output).
    el.style.whiteSpace = 'normal';
  }} catch (e) {{
    el.textContent = text;       // never break the chat on a render error
    el.style.whiteSpace = '';    // revert to the CSS pre-wrap for plain text
  }}
}}
function addMsg(role, text, streaming) {{
  const div = document.createElement('div');
  div.className = `msg ${{role}}${{streaming ? ' streaming' : ''}}`;
  if (role === 'assistant') renderAssistantInto(div, text);
  else div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}}

function attachAssistantTrace(afterEl, traceId, status) {{
  if (!traceId || !afterEl) return;
  const prior = afterEl.nextElementSibling;
  if (prior && prior.classList.contains('trace-inline')) prior.remove();
  const row = document.createElement('div');
  row.className = 'trace-inline';
  const shortId = traceId.length > 18 ? traceId.slice(0, 18) + '...' : traceId;
  const state = status === 'error' ? 'errored' : 'captured';
  row.innerHTML =
    `<span>MLflow trace ${{state}}</span>` +
    `<button type="button" title="Show this trace in the APX trace panel">` +
    `<code>${{escHtml(shortId)}}</code></button>` +
    `<a href="/_apx/traces/${{encodeURIComponent(traceId)}}" target="_blank" title="Open full trace">full</a>`;
  row.querySelector('button').onclick = () => {{
    switchTab('trace', document.querySelectorAll('.panel-tabs button')[2]);
    finalizeTrace(traceId, status, {{ emitEvents: false }});
  }};
  afterEl.after(row);
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
    // Responses-API output array: find the last message item's text.
    const msgs = value.filter(m => m && typeof m === 'object' && m.type === 'message');
    if (msgs.length) return extractMsg(msgs[msgs.length - 1].content);
    // Generic: last item with a content field.
    for (let i = value.length - 1; i >= 0; i--) {{
      const m = value[i];
      if (m && typeof m === 'object' && 'content' in m) return extractMsg(m.content);
    }}
    return value.map(extractMsg).filter(Boolean).join(', ').slice(0, 300);
  }}
  if (typeof value === 'object') {{
    // Responses-API output_text block.
    if (value.type === 'output_text' && value.text) return value.text.slice(0, 400);
    // Grounded-tool result (e.g. knowledge_assistant): show the answer + a
    // citation count, not the raw {{question, answer, citations}} dict.
    if ('answer' in value) {{
      const n = Array.isArray(value.citations) ? value.citations.length : 0;
      return String(value.answer).slice(0, 400) + (n ? ` [${{n}} citation${{n > 1 ? 's' : ''}}]` : '');
    }}
    for (const k of ['content', 'text', 'output_text', 'message', 'messages', 'choices', 'output', 'input']) {{
      if (k in value && value[k] != null) return extractMsg(value[k]);
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
    return null;
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
      return traceId;
    }}
    traceBody.innerHTML = '';
    const SPAN_COLORS = {{LLM:'#22d3ee',TOOL:'#facc15',CHAIN:'#a78bfa',AGENT:'#60b0ff',OTHER:'#94a3b8'}};
    function spanTypeShort(t) {{
      t = (t||'').toUpperCase();
      if (['LLM','CHAT_MODEL','EMBEDDING'].includes(t)) return 'LLM';
      if (['TOOL','RETRIEVER'].includes(t)) return 'TOOL';
      if (t === 'CHAIN') return 'CHAIN';
      if (t === 'AGENT') return 'AGENT';
      return 'OTHER';
    }}

    // ── Summary view: LLM + TOOL spans in start-time order ──
    // Normalization in _serialize_trace_spans ensures Responses-API traces
    // already have CHAT_MODEL spans and synthetic TOOL children, so one path
    // handles all formats.
    const ordered = [...data.spans].sort((a,b) => (a.start_time_ns||0)-(b.start_time_ns||0));
    // Framework-plumbing CHAIN spans that aren't meaningful sub-agents. Named
    // CHAIN/AGENT spans (router, knowledge_assistant, …) ARE sub-agent
    // boundaries and belong in the summary alongside LLM + TOOL steps.
    const CHAIN_NOISE = new Set(['LangGraph','RunnableCallable','model','tools','compile_to_langgraph']);
    function isSubAgent(s) {{
      const t = spanTypeShort(s.span_type);
      const n = s.name || '';
      // Real sub-agents are graph-node names (bare identifiers: router,
      // knowledge_assistant, …). Exclude framework wrappers: the noise set,
      // dotted names (ApxResponsesAgent.invoke), and request/transport spans
      // with spaces or slashes (POST /responses).
      return (t === 'AGENT' || t === 'CHAIN') && n
        && !CHAIN_NOISE.has(n) && !n.includes('.') && !n.includes(' ') && !n.includes('/');
    }}
    const keySpans = ordered.filter(s => {{
      const t = spanTypeShort(s.span_type);
      return t === 'LLM' || t === 'TOOL' || isSubAgent(s);
    }});

    function makeStepCard(typeLabel, color, name, dur, isErr) {{
      const card = document.createElement('div');
      card.className = 'span-step';
      card.style.cssText = 'margin-bottom:6px;';
      card.innerHTML =
        `<div class="step-header">` +
        `<span style="font-size:10px;font-weight:700;padding:1px 5px;border-radius:3px;background:${{color}}22;color:${{color}}">${{typeLabel}}</span>` +
        `<span class="who" style="color:${{color}};font-size:12px;font-weight:600;margin-left:6px">${{escHtml(name)}}</span>` +
        (isErr ? `<span style="color:#f87171;font-size:10px;margin-left:6px">ERR</span>` : '') +
        (dur ? `<span class="dur" style="margin-left:auto">${{dur}}</span>` : '') +
        `</div>`;
      return card;
    }}
    function addBubble(card, val, cls) {{
      const msg = extractMsg(val);
      if (!msg) return;
      const b = document.createElement('div');
      b.className = `span-bubble ${{cls}}`;
      b.style.cssText = 'font-size:11px;margin-top:4px;';
      b.textContent = msg.slice(0, 200) + (msg.length > 200 ? '…' : '');
      card.appendChild(b);
    }}

    if (!keySpans.length) {{
      traceBody.innerHTML = '<div style="color:#555;font-size:12px;padding:8px 0">No tool or model spans found.</div>';
    }}
    for (const s of keySpans) {{
      const type = spanTypeShort(s.span_type);
      const sub = isSubAgent(s);
      const label = sub ? 'SUB-AGENT' : type;
      const color = sub ? SPAN_COLORS.AGENT : (SPAN_COLORS[type] || '#888');
      const dur = s.duration_ms != null ? `${{Math.round(s.duration_ms)}}ms` : '';
      const isErr = (s.status||'').toUpperCase().includes('ERR');
      const card = makeStepCard(label, color, s.name, dur, isErr);
      if (sub) {{
        // Sub-agent boundary: show only what it was asked, not its full plumbing.
        addBubble(card, s.inputs, 'agent-ask');
      }} else {{
        addBubble(card, s.inputs,  type === 'TOOL' ? 'tool-in' : 'agent-ask');
        addBubble(card, s.outputs, type === 'TOOL' ? 'tool-out' : 'llm-reply');
      }}
      traceBody.appendChild(card);
    }}

    if (!keySpans.length) {{
      traceBody.innerHTML = '<div style="color:#555;font-size:12px;padding:8px 0">No LLM or tool spans found.</div>';
    }}

    // ── Full tree (collapsed by default) ──
    const toggle = document.createElement('button');
    toggle.textContent = '▸ Full trace';
    toggle.style.cssText = 'margin-top:8px;background:none;border:1px solid #333;color:#666;font-size:11px;padding:2px 8px;border-radius:3px;cursor:pointer;';
    const treeDiv = document.createElement('div');
    treeDiv.style.display = 'none';
    toggle.onclick = () => {{
      const open = treeDiv.style.display !== 'none';
      treeDiv.style.display = open ? 'none' : 'block';
      toggle.textContent = open ? '▸ Full trace' : '▾ Full trace';
    }};
    traceBody.appendChild(toggle);
    traceBody.appendChild(treeDiv);

    // Build parent→children for full tree
    const byParent = {{}};
    const roots = [];
    for (const s of data.spans) {{
      if (s.parent_id) (byParent[s.parent_id] = byParent[s.parent_id] || []).push(s);
      else roots.push(s);
    }}
    function renderSpanNode(s, depth) {{
      const type = spanTypeShort(s.span_type);
      const color = SPAN_COLORS[type] || '#888';
      const dur = s.duration_ms != null ? `${{Math.round(s.duration_ms)}}ms` : '';
      const isErr = (s.status||'').toUpperCase().includes('ERR');
      const wrap = document.createElement('div');
      wrap.style.cssText = `padding-left:${{depth*12}}px;margin-bottom:2px;`;
      const card = document.createElement('div');
      card.className = 'span-step';
      card.innerHTML =
        `<div class="step-header">` +
        `<span style="font-size:9px;font-weight:700;padding:1px 4px;border-radius:2px;background:${{color}}22;color:${{color}}">${{type}}</span>` +
        `<span class="who" style="color:${{color}};font-size:11px;font-weight:600;margin-left:5px">${{escHtml(s.name)}}</span>` +
        (isErr ? `<span style="color:#f87171;font-size:9px;margin-left:5px">ERR</span>` : '') +
        (dur ? `<span class="dur" style="margin-left:auto;font-size:10px">${{dur}}</span>` : '') +
        `</div>`;
      wrap.appendChild(card);
      for (const child of (byParent[s.span_id] || [])) {{
        wrap.appendChild(renderSpanNode(child, depth + 1));
      }}
      return wrap;
    }}
    for (const root of roots) treeDiv.appendChild(renderSpanNode(root, 0));

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
        const gid = s.span_id || s.name;
        addToolCall(gid, s.name, fmt(args).slice(0, 120), {{ arguments: args }});
        addToolResponse(gid, s.name, dur, {{ result: result }}, isErr);
      }}
    }}
  }} catch(e) {{
    traceBody.innerHTML = `<div style="color:#f87171;font-size:12px">${{escHtml(e.message)}}</div>`;
  }}
  return traceId;
}}

function addToolPills(trace) {{
  const container = document.createElement('div');
  container.className = 'tool-pills';
  let _pi = 0;
  for (const t of trace) {{
    const gid = 'pill-' + (_pi++);
    const isErr = t.result && typeof t.result === 'object' && 'error' in t.result;
    const call = document.createElement('span');
    call.className = 'tool-pill call';
    call.innerHTML = `<span class="icon">⚡</span>${{t.name}}`;
    call.dataset.tip = JSON.stringify(t.args, null, 2);
    call.onmouseenter = showTip;
    call.onmouseleave = hideTip;
    const callEv = addToolCall(gid, t.name, fmt(t.args).slice(0, 120), {{ arguments: t.args }});
    call.onclick = () => showDetail(callEv, null);
    container.appendChild(call);
    const res = document.createElement('span');
    res.className = `tool-pill ${{isErr ? 'error' : 'result'}}`;
    res.innerHTML = `<span class="icon">${{isErr ? '✗' : '✓'}}</span>${{t.name}}<span class="ms">${{t.ms}}ms</span>`;
    res.dataset.tip = fmt(t.result);
    res.onmouseenter = showTip;
    res.onmouseleave = hideTip;
    const resEv = addToolResponse(gid, t.name, `${{t.ms}}ms`, {{ result: t.result }}, isErr);
    res.onclick = () => showDetail(resEv, null);
    container.appendChild(res);
  }}
  chat.appendChild(container);
  chat.scrollTop = chat.scrollHeight;
}}

// ── Tool detail formatters ──
// SQL keyword highlighter.
function sqlHL(sql) {{
  const kws = /\\b(SELECT|FROM|WHERE|AND|OR|NOT|NULL|TRUE|FALSE|AS|IN|ON|JOIN|LEFT|RIGHT|INNER|ORDER|BY|LIMIT|GROUP|HAVING|DISTINCT|CASE|WHEN|THEN|ELSE|END|INSERT|UPDATE|DELETE|CREATE|DROP|TABLE|VIEW|WITH|IS|LIKE|BETWEEN|EXISTS|ALL|ANY|UNION|VALUES|SET|INTO|OUTER|CROSS|FULL|ASC|DESC|CAST|OVER|PARTITION|NULLIF|COALESCE|COUNT|SUM|AVG|MIN|MAX|RANK)\\b/gi;
  return sql.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/('(?:[^']|'')*')/g, s => `<span style="color:#ce9178">${{s}}</span>`)
    .replace(kws, k => `<span style="color:#569cd6;font-weight:600">${{k}}</span>`);
}}
function fmtSql(sql) {{
  return `<pre style="background:#1e1e1e;font-family:ui-monospace,monospace;font-size:11px;line-height:1.6;padding:10px 12px;border-radius:4px;overflow:auto;margin:0">${{sqlHL(sql)}}</pre>`;
}}
function fmtTable(rows) {{
  if (!rows || !rows.length) return '<div style="color:#555;font-size:12px;padding:8px">No rows returned.</div>';
  const cols = Object.keys(rows[0]);
  const th = cols.map(c => `<th style="padding:4px 8px;text-align:left;border-bottom:1px solid #2a2a2a;color:#9cdcfe;font-weight:500;white-space:nowrap">${{esc(c)}}</th>`).join('');
  const trs = rows.map(r =>
    `<tr>${{cols.map(c => `<td style="padding:3px 8px;border-bottom:1px solid #1a1a1a;color:#ccc;white-space:nowrap">${{esc(String(r[c] ?? ''))}}</td>`).join('')}}</tr>`
  ).join('');
  return `<div style="overflow:auto"><table style="border-collapse:collapse;font-family:ui-monospace,monospace;font-size:11px;width:100%"><thead><tr>${{th}}</tr></thead><tbody>${{trs}}</tbody></table><div style="color:#555;font-size:11px;padding:4px 8px">${{rows.length}} row${{rows.length !== 1 ? 's' : ''}}</div></div>`;
}}
function fmtReq(rawStr) {{
  try {{
    const obj = JSON.parse(rawStr);
    const pretty = JSON.stringify(obj, null, 2);
    return `<pre style="background:#1e1e1e;font-family:ui-monospace,monospace;font-size:11px;line-height:1.5;padding:10px 12px;border-radius:4px;overflow:auto;margin:0;color:#ccc">${{esc(pretty)}}</pre>`;
  }} catch {{}}
  return `<pre class="step-detail-pre">${{esc(rawStr)}}</pre>`;
}}
function fmtResp(rawStr) {{
  try {{
    const obj = JSON.parse(rawStr);
    if (obj._sql || Array.isArray(obj.data)) {{
      let html = '';
      if (obj._sql) {{
        // UC function link header (SQL itself is shown in the Request section)
        const ucMatch = obj._sql.match(/FROM\\s+([\\w]+)\\.([\\w]+)\\.([\\w]+)\\s*\\(/i);
        if (ucMatch) {{
          const [, cat, schema, fn] = ucMatch;
          const fqn = `${{cat}}.${{schema}}.${{fn}}`;
          // Link to schema page — function URLs return "table not found" in UC explorer
          const ucUrl = apxHost ? `${{apxHost}}/explore/data/${{cat}}/${{schema}}` : null;
          const fnLink = ucUrl
            ? `<a href="${{ucUrl}}" target="_blank" style="color:#60b0ff;font-family:ui-monospace,monospace;font-size:11px;text-decoration:none;margin-left:8px">${{esc(fqn)}} ↗</a>`
            : `<span style="color:#9cdcfe;font-family:ui-monospace,monospace;font-size:11px;margin-left:8px">${{esc(fqn)}}</span>`;
          const sqlHeader = '<span style="color:#555;font-size:10px;text-transform:uppercase;letter-spacing:.05em">Unity Catalog Function</span>' + fnLink;
          html += `<div style="display:flex;align-items:center;padding:8px 0 4px">${{sqlHeader}}</div>`;
        }}
      }}
      if (Array.isArray(obj.data)) html += `<div style="color:#555;font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding:8px 0 4px">Results${{obj._timing ? ' · ' + obj._timing : ''}}</div>${{fmtTable(obj.data)}}`;
      return html;
    }}
  }} catch {{}}
  try {{
    const pretty = JSON.stringify(JSON.parse(rawStr), null, 2);
    return `<pre style="background:#1e1e1e;font-family:ui-monospace,monospace;font-size:11px;line-height:1.5;padding:10px 12px;border-radius:4px;overflow:auto;margin:0;color:#ccc">${{esc(pretty)}}</pre>`;
  }} catch {{}}
  return `<pre class="step-detail-pre">${{esc(rawStr)}}</pre>`;
}}

// ── Inline thinking-steps ──
// Live tool-call rows rendered in the transcript above the answer bubble.
// Keyed by callId so the function_call (running) and its function_call_output
// (done/error) update the SAME row. The function_call_output item carries no
// `name`, so we stash the tool name on the row when it's created and reuse it.
const inlineSteps = {{}};  // callId -> row element (reset per send, see send handler)
function renderInlineStep(stepsContainer, callId, opts) {{
  // opts: {{ name, phase: 'running'|'done'|'error', request, response }}
  // The REQUEST (tool args — for a SQL tool, the query itself) and the
  // RESPONSE (rows) are kept as separate persistent sections so the query
  // is never overwritten by its result.
  let row = inlineSteps[callId];
  if (!row) {{
    row = document.createElement('div');
    row.className = 'inline-step';
    row.innerHTML = '<div class="inline-step-head"></div><div class="inline-step-detail"></div>';
    row.querySelector('.inline-step-head').onclick = () => row.classList.toggle('open');
    stepsContainer.appendChild(row);
    inlineSteps[callId] = row;
  }}
  if (opts.name) row.dataset.toolName = opts.name;
  if (opts.request != null) row._req = opts.request;
  if (opts.response != null) row._resp = opts.response;
  const name = opts.name || row.dataset.toolName || 'tool';
  const icon = opts.phase === 'running' ? '⚙' : (opts.phase === 'error' ? '✗' : '✓');
  const label = opts.phase === 'running' ? 'running…' : (opts.phase === 'error' ? 'error - details' : 'done - details');
  row.classList.toggle('error', opts.phase === 'error');
  if (document.body.classList.contains('apx-embed') && opts.phase !== 'running') {{
    row.classList.add('open');
  }}
  row.querySelector('.inline-step-head').innerHTML =
    `<span class="step-icon">${{icon}}</span><span class="step-name">${{esc(name)}}</span>`
    + `<span class="step-label">${{label}}</span><span class="step-caret">›</span>`;
  const detail = row.querySelector('.inline-step-detail');
  detail.innerHTML = '';
  // Show the request unless it's empty/no-arg ('{{}}'): a no-arg tool has no
  // query to show, so we skip the Request section rather than print '{{}}'.
  if (row._req != null && row._req !== '' && row._req.trim() !== '{{}}') {{
    // For SQL tools: show the generated query as the request (not the raw args JSON)
    let reqBody = '';
    if (row._resp) {{
      try {{
        const rp = JSON.parse(row._resp);
        if (rp._sql) reqBody = fmtSql(rp._sql);
      }} catch {{}}
    }}
    if (!reqBody) reqBody = fmtReq(row._req);
    detail.insertAdjacentHTML('beforeend',
      `<div class="step-detail-label">Request</div>${{reqBody}}`);
  }}
  if (row._resp != null) {{
    detail.insertAdjacentHTML('beforeend',
      `<div class="step-detail-label">Response</div>${{fmtResp(row._resp)}}`);
  }}
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
  // Live tool steps render into their own container ABOVE the answer bubble.
  for (const k in inlineSteps) delete inlineSteps[k];   // reset per send
  for (const k in toolGroups) delete toolGroups[k];
  for (const k in memCallBodies) delete memCallBodies[k];
  const stepsContainer = document.createElement('div');
  stepsContainer.className = 'inline-steps';
  chat.insertBefore(stepsContainer, assistantDiv);       // steps appear ABOVE the answer
  let full = '';
  let pendingTrace = null;
  let traceId = null;
  let traceStatus = 'completed';
  // Tool calls are surfaced live from the stream (below). When that happens we
  // tell finalizeTrace NOT to re-harvest them from the trace spans, so the
  // Events panel doesn't double up on workspaces where the trace also loads.
  let toolEventsFromStream = false;

  try {{
    const res = await fetch('/responses', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json', 'x-return-trace-id': 'true' }},
      body: JSON.stringify({{ input: history, stream: true, custom_inputs: {{ thread_id: devThreadId }} }}),
    }});
    if (!res.ok) throw new Error(`${{res.status}} ${{await res.text()}}`);
    traceId = res.headers.get('x-apx-trace-id') || traceId;

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
            renderAssistantInto(assistantDiv, full);
            chat.scrollTop = chat.scrollHeight;
          }} else if (ptype === 'response.output_item.done') {{
            const item = payload.item || {{}};
            if (item.type === 'message' && Array.isArray(item.content)) {{
              for (const part of item.content) {{
                if (part.type === 'output_text' && part.text) {{
                  full += part.text;
                  renderAssistantInto(assistantDiv, full);
                  chat.scrollTop = chat.scrollHeight;
                }}
              }}
            }} else if (item.type === 'function_call') {{
              // Surface the tool call (+ its SQL/args) live from the stream —
              // no trace fetch, so it works even when artifact-storage egress
              // is blocked. Detail pane shows the full arguments on click.
              toolEventsFromStream = true;
              const argStr = typeof item.arguments === 'string'
                ? item.arguments : JSON.stringify(item.arguments || {{}});
              // Group the call + its response by call_id in the Events panel.
              addToolCall(item.call_id || item.id || item.name, item.name,
                argStr.slice(0, 120), {{ arguments: argStr }});
              // Also render the call live in the transcript as a step row.
              // Pass the args as `request` (for a SQL tool this IS the query),
              // kept separate from the result so it isn't overwritten on done.
              renderInlineStep(stepsContainer, item.call_id || item.id || item.name,
                {{ name: item.name, phase: 'running', request: argStr }});
            }} else if (item.type === 'function_call_output') {{
              toolEventsFromStream = true;
              const outStr = typeof item.output === 'string'
                ? item.output : JSON.stringify(item.output || '');
              const isErr = /\"error\"|\berror\b/i.test(outStr);
              // Fill the response into the SAME group as its call (by call_id).
              addToolResponse(item.call_id || item.id || item.name, item.name,
                outStr.slice(0, 120), {{ output: outStr }}, isErr);
              // Update the SAME step row (shared call_id) running → done/error.
              // Pass item.name unchanged (undefined on output items) so the row's
              // stashed tool name survives — a truthy fallback would overwrite it.
              renderInlineStep(stepsContainer, item.call_id || item.id || item.name,
                {{ name: item.name, phase: isErr ? 'error' : 'done', response: outStr }});
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
              if (full) renderAssistantInto(assistantDiv, full);
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
    renderAssistantInto(assistantDiv, full);
    traceStatus = 'error';
  }}

  assistantDiv.classList.remove('streaming');
  addEvent('assistant', full.slice(0, 80) + (full.length > 80 ? '…' : ''), null, {{ content: full }});
  history.push({{ role: 'assistant', content: full }});
  traceId = await finalizeTrace(traceId, traceStatus, {{ emitEvents: !toolEventsFromStream }});
  attachAssistantTrace(assistantDiv, traceId, traceStatus);
  sendBtn.disabled = false;
  inputEl.focus();
  // Refresh history list so the new/updated conversation appears.
  loadConversationHistory();
  // Surface any ASK-policy approval requests raised during this turn.
  checkPendingApprovals();
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
