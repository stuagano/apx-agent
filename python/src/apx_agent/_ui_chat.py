"""Dev UI — /_apx/agent unified shell, chat interface, OpenAPI spec builder, and /_apx/tools inspector."""

from __future__ import annotations

import os
from typing import Any

from ._models import AgentContext, AgentTool, workflow_prompts, workflows_for_context
from ._ui_nav import _apx_nav_links, _deploy_overlay_html

from ._ui_theme import apx_theme_style

# Tabs exposed by the unified shell at /_apx/agent. Each entry is
# (slug, label, iframe URL). The shell defaults to the first tab. To
# add a tab, append here — the shell auto-renders it and the URL
# fragment-router handles selection.
_UNSET_ENV = ""  # optional UI env vars (catalog, warehouse) resolve to empty when not set

_UNIFIED_TABS: tuple[tuple[str, str, str], ...] = (
    ("chat", "Chat", "/_apx/chat?shell=1"),  # the marker keeps the shell's own load from redirecting
    ("edit", "Edit", "/_apx/edit"),
    ("tools", "Tools", "/_apx/tools"),
    ("eval", "Eval", "/_apx/eval"),
    # "setup" is intentionally not a shell tab — its data-source + tool
    # generation flow is reached from the Edit page's "✨ From data" modal.
    ("discover", "Discover", "/_apx/discover"),
    ("hub", "Hub", "/_apx/hub"),
    ("grounding", "Grounding", "/_apx/grounding"),
    ("knowledge", "Knowledge", "/_apx/knowledge"),
    ("probe", "Probe", "/_apx/probe"),
)


def standalone_chat_redirect(request: Any) -> Any:
    """Send a standalone /_apx/chat to the builder console's Chat tab; None for a framed load.

    Two chat pages, not three: "/" for users, /_apx/agent for builders. The shell's Chat tab
    marks its load with ?shell=1 and the topology dock with ?embed=1.
    """
    from fastapi.responses import RedirectResponse

    params = request.query_params
    if params.get("shell") == "1" or params.get("embed") == "1":
        return None
    query = f"?{request.url.query}" if request.url.query else ""  # e.g. ?token= for the dev token
    return RedirectResponse(f"/_apx/agent{query}#chat")


# History/Trace/Eval read the conversation store and the legacy send hooks, which the durable
# transport doesn't feed; hidden on native pages rather than shown empty or broken. Events is fed
# from the durable event stream (see _ui_native_chat.py).
NATIVE_PANEL_HIDE = (
    "<style>#btn-deploy,#setup-banner,"
    + ",".join(f".panel-tabs button[onclick*=\"'{t}'\"],#tab-{t}" for t in ("history", "trace", "eval"))
    + "{display:none!important}</style>"
)
NATIVE_PANEL_OPEN_TOOLS = (
    "<script>switchTab('tools', document.querySelector(\".panel-tabs button[onclick*=\\\"'tools'\\\"]\"));</script>"
)


def _render_unified_shell(ctx: AgentContext | None, *, hidden_tabs: frozenset[str] = frozenset()) -> str:
    """Render the tabbed shell that hosts every /_apx/* page in one iframe.

    Each tab swaps the iframe src + updates the URL hash so bookmarks +
    browser back/forward work. The inner pages detect iframe context via
    ``window.self !== window.top`` and hide their own nav bar (see
    ``_apx_nav_html``) so the shell's tab strip is the only nav.
    """
    agent_name = ctx.config.name if ctx else "Agent"
    agent_desc = ctx.config.description if ctx else ""

    tabs = [tab for tab in _UNIFIED_TABS if tab[0] not in hidden_tabs]
    tab_buttons = "".join(
        f'<button class="tab" data-tab="{slug}" data-src="{src}">{label}</button>'
        for slug, label, src in tabs
    )
    default_slug = _UNIFIED_TABS[0][0]
    default_src = _UNIFIED_TABS[0][2]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{agent_name} · APX dev</title>
  {apx_theme_style()}
  <style>
    :root {{
      --bg: var(--apx-bg); --panel: var(--apx-panel); --border: var(--apx-border); --border-strong: var(--apx-border);
      --text: var(--apx-text); --text-muted: var(--apx-muted); --accent: var(--apx-accent);
      --accent-bg: var(--apx-panel-2); --accent-border: var(--apx-panel-2);
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
    .util-btn:hover {{ color: var(--text); border-color: var(--apx-muted); }}
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
    .util-menu-panel a .path {{ margin-left: auto; color: var(--apx-muted); font-size: 10px;
                                font-family: ui-monospace, monospace; }}
    #btn-sidebar-toggle {{ background: none; border: none; color: var(--apx-muted); font-size: 16px;
                           cursor: pointer; padding: 4px 8px; border-radius: 5px; line-height: 1;
                           flex-shrink: 0; }}
    #btn-sidebar-toggle:hover {{ color: var(--apx-muted); background: var(--apx-border); }}
    /* Layout: sidebar + content */
    .shell-body {{ flex: 1; display: flex; min-height: 0; }}
    #sidebar {{ width: 220px; flex-shrink: 0; background: var(--apx-bg);
                border-right: 1px solid var(--apx-border); display: flex; flex-direction: column;
                overflow: hidden; transition: width .18s ease; }}
    body.sidebar-off #sidebar {{ width: 0; }}
    #sidebar-scroll {{ flex: 1; overflow-y: auto; padding: 8px 0 16px; }}
    /* Sidebar identity block */
    .sb-identity {{ padding: 10px 14px 8px; border-bottom: 1px solid var(--apx-border); margin-bottom: 6px; }}
    .sb-host {{ font-family: ui-monospace,monospace; font-size: 10px; color: var(--apx-accent);
                text-decoration: none; display: block; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }}
    .sb-host:hover {{ text-decoration: underline; }}
    .sb-user {{ font-size: 10px; color: var(--apx-muted); margin-top: 2px; overflow: hidden;
                text-overflow: ellipsis; white-space: nowrap; }}
    /* Section label */
    .sb-section {{ font-size: 10px; color: var(--apx-muted); text-transform: uppercase;
                   letter-spacing: .08em; padding: 10px 14px 4px; }}
    /* Nav items */
    .sb-item {{ display: flex; align-items: center; gap: 10px; padding: 7px 14px;
                cursor: pointer; border-radius: 0; user-select: none;
                color: var(--apx-muted); font-size: 13px; border: none; background: none;
                width: 100%; text-align: left; text-decoration: none; }}
    .sb-item:hover {{ background: var(--apx-border); color: var(--apx-muted); }}
    .sb-item.active {{ background: var(--apx-panel-2); color: var(--accent); }}
    .sb-icon {{ width: 18px; flex-shrink: 0; text-align: center; font-size: 14px;
                line-height: 1; opacity: .75; }}
    .sb-item:hover .sb-icon, .sb-item.active .sb-icon {{ opacity: 1; }}
    .sb-label {{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    /* Data tree inside sidebar */
    .sb-tree {{ padding: 0 6px; }}
    .tree-row {{ display: flex; align-items: center; gap: 7px; padding: 6px 8px;
                 border-radius: 5px; cursor: pointer; user-select: none; }}
    .tree-row:hover {{ background: var(--apx-border); color: var(--apx-muted); }}
    .tree-chevron {{ flex-shrink: 0; width: 10px; color: var(--apx-border); transition: transform .15s;
                     font-size: 9px; line-height: 1; }}
    .tree-row.open > .tree-chevron {{ transform: rotate(90deg); color: var(--apx-muted); }}
    .tree-icon {{ flex-shrink: 0; font-size: 13px; line-height: 1; width: 16px;
                  text-align: center; opacity: .8; }}
    .tree-name {{ font-family: ui-monospace, monospace; font-size: 11px; flex: 1;
                  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .tree-cat > .tree-name {{ color: var(--apx-text); font-weight: 500; }}
    .tree-schemas {{ padding-left: 22px; position: relative; }}
    .tree-schemas::before {{ content: ''; position: absolute; left: 13px; top: 0;
                              bottom: 4px; width: 1px; background: var(--apx-border); }}
    .tree-sch > .tree-name {{ color: var(--apx-muted); }}
    .tree-tables {{ padding-left: 22px; position: relative; }}
    .tree-tables::before {{ content: ''; position: absolute; left: 13px; top: 0;
                             bottom: 4px; width: 1px; background: var(--apx-border); }}
    .tree-tbl {{ display: flex; align-items: center; gap: 7px; padding: 5px 8px;
                 border-radius: 4px; cursor: pointer; user-select: none; }}
    .tree-tbl:hover {{ background: var(--apx-panel-2); }}
    .tree-tbl:hover > .tree-name {{ color: var(--apx-ok); }}
    .tree-tbl > .tree-name {{ color: var(--apx-muted); font-size: 10px; transition: color .1s; }}
    .tree-loading {{ padding: 4px 8px; color: var(--apx-border); font-size: 11px; font-style: italic; }}
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
          <div class="sb-user" style="color:var(--apx-border)">Loading…</div>
        </div>
        <div id="sb-links"></div>
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
        renderSidebarLinks(d);
      }} catch(e) {{
        const id = document.getElementById("sb-identity");
        if (id) id.innerHTML = '<div class="sb-user">Unavailable</div>';
      }}
    }}
    // Rows are built with textContent: declared names are never parsed as markup.
    function sbSection(id, title, rows) {{
      const box = document.getElementById(id);
      if (!box || !rows.length) return;
      const head = document.createElement("div");
      head.className = "sb-section";
      head.textContent = title;
      box.replaceChildren(head, ...rows);
    }}
    function sbRow(icon, label, href) {{
      const row = document.createElement(href ? "a" : "div");
      row.className = "sb-item";
      if (href) {{ row.href = href; row.target = "_blank"; row.rel = "noopener"; }}
      const ico = document.createElement("span");
      ico.className = "sb-icon";
      ico.textContent = icon;
      const text = document.createElement("span");
      text.className = "sb-label";
      text.textContent = label;
      text.title = label;
      row.append(ico, text);
      return row;
    }}
    function renderSidebarLinks(d) {{
      const rows = [];
      if (d.app_url) rows.push(sbRow("▣", "Databricks App ↗", d.app_url));
      if (d.experiment_url) rows.push(sbRow("◷", "MLflow experiment ↗", d.experiment_url));
      sbSection("sb-links", "Workspace", rows);
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
              ? tables.map(t => `<div class="tree-tbl" onclick="selectTable('${{catalog}}.${{schema}}.${{t}}')" title="Ask about ${{t}}"><span class="tree-icon" style="font-size:9px;color:var(--apx-border)">▦</span><span class="tree-name">${{t}}</span></div>`).join("")
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
    native: bool = False,
) -> str:
    """Eval page — run eval cases against the live agent with LLM-as-judge scoring.

    ``native`` hosts serve the durable ``/api/invocations`` contract, not ``/invocations``."""
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
  {apx_theme_style()}
  <style>
    :root {{ --bg: var(--apx-bg); --panel: var(--apx-panel); --border: var(--apx-border); --text: var(--apx-text);
             --muted: var(--apx-muted); --accent: var(--apx-accent); --accent-bg: var(--apx-panel-2); --accent-border: var(--apx-panel-2); }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--text);
            font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; line-height: 1.5; }}
    .container {{ max-width: 900px; margin: 0 auto; padding: 24px 20px; }}
    h1 {{ font-size: 17px; font-weight: 600; margin: 0 0 4px; }}
    .meta {{ color: var(--muted); font-size: 12px; margin-bottom: 16px; }}
    .meta code {{ background: var(--apx-border); padding: 1px 5px; border-radius: 3px; color: var(--apx-muted); }}
    .toolbar {{ display: flex; gap: 8px; align-items: center; margin-bottom: 16px; }}
    .btn {{ padding: 5px 14px; border-radius: 5px; font-size: 12px; cursor: pointer; border: 1px solid; }}
    .btn-run {{ background: var(--accent-bg); color: var(--accent); border-color: var(--accent-border); }}
    .btn-run:disabled {{ opacity: .4; cursor: default; }}
    .btn-reset {{ background: transparent; color: var(--muted); border-color: var(--apx-border); }}
    #status {{ font-size: 11px; color: var(--muted); }}
    .progress {{ height: 2px; background: var(--apx-border); margin-bottom: 16px; border-radius: 1px; }}
    .progress-fill {{ height: 100%; background: var(--apx-accent); width: 0%; transition: width .3s; border-radius: 1px; }}
    .banner {{ padding: 10px 14px; border-radius: 6px; margin-bottom: 16px;
               border: 1px solid var(--border); background: var(--panel); }}
    .banner-info {{ border-color: var(--accent-border); background: var(--accent-bg); color: var(--apx-text); }}
    .banner-err {{ border-color: var(--apx-border); background: var(--apx-panel-2); color: var(--apx-err); }}
    .case {{ border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px;
             margin-bottom: 8px; background: var(--panel); }}
    .case.pass {{ border-color: var(--apx-panel-2); }}
    .case.fail {{ border-color: var(--apx-panel-2); }}
    .case-head {{ display: flex; gap: 8px; align-items: baseline; margin-bottom: 6px; }}
    .case-n {{ color: var(--muted); font-size: 11px; min-width: 24px; }}
    .case-q {{ font-weight: 500; flex: 1; }}
    .badge {{ font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 3px; }}
    .badge-pass {{ background: var(--apx-panel-2); color: var(--apx-ok); }}
    .badge-fail {{ background: var(--apx-border); color: var(--apx-err); }}
    .badge-running {{ background: var(--apx-panel-2); color: var(--apx-text); }}
    .case-row {{ display: flex; gap: 8px; padding: 2px 0 2px 32px; font-size: 12px; color: var(--muted); }}
    .label {{ min-width: 64px; color: var(--apx-muted); text-transform: uppercase; font-size: 10px; letter-spacing: .4px; padding-top: 2px; }}
    .case-response {{ padding: 6px 14px 6px 32px; font-size: 12px; color: var(--apx-muted); white-space: pre-wrap; word-break: break-word; line-height: 1.6; border-left: 2px solid var(--apx-border); margin: 4px 14px 4px 32px; padding-left: 10px; }}
    .case-reason {{ padding: 2px 32px 6px; font-size: 11px; color: var(--apx-muted); font-style: italic; }}
    .run-btn {{ background: transparent; border: none; color: var(--accent); cursor: pointer;
                font-size: 13px; padding: 0 4px; opacity: .7; }}
    .run-btn:hover {{ opacity: 1; }}
    .add-section {{ margin-top: 24px; border-top: 1px solid var(--border); padding-top: 16px; }}
    .add-section h2 {{ font-size: 13px; font-weight: 600; margin: 0 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; }}
    textarea, input {{ width: 100%; background: var(--apx-panel); border: 1px solid var(--apx-border); color: var(--text);
                        border-radius: 5px; padding: 8px 10px; font-size: 12px; font-family: inherit;
                        resize: none; outline: none; margin-bottom: 6px; }}
    textarea:focus, input:focus {{ border-color: var(--apx-muted); }}
    .btn-add {{ background: transparent; color: var(--muted); border: 1px solid var(--apx-border);
                border-radius: 5px; padding: 5px 12px; font-size: 11px; cursor: pointer; }}
    .btn-add:hover {{ color: var(--text); border-color: var(--apx-muted); }}
  </style>
</head>
<body>
<div class="container">
  <h1>Eval</h1>
  <div class="meta">{case_count} case{'' if case_count == 1 else 's'} &middot; <code>{file_label}</code></div>
  {empty_banner}
  <div class="toolbar">
    <button class="btn btn-run" id="run-all">&#9654; Run All</button>
    <button class="btn btn-reset" id="reset">&#8634; Reset</button>
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
      then run MemAlign to distill guidelines from your ratings into an aligned judge.
    </p>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <input id="la-judge" placeholder="Judge name (e.g. quality)" style="max-width:220px;margin:0" />
      <button class="btn btn-run" id="la-align-btn" onclick="labelAlign()">Run alignment</button>
    </div>
    <div id="la-status" style="font-size:12px;color:var(--muted);min-height:18px"></div>
    <div id="la-history" style="margin-top:14px"></div>
  </div>
</div>
<script>
let rows = {cases_json};
const NATIVE = {_json.dumps(native)};

// A durable host: submit the case in a fresh session (no state leaks between cases),
// wait for the run to finish, and read the answer the native chat shows.
async function runNative(question) {{
  const id = crypto.randomUUID();
  const post = await fetch('/api/invocations', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{id, session_id: crypto.randomUUID(), background: true,
      input: {{messages: [{{role: 'user', content: question}}]}}}}),
  }});
  if (!post.ok) throw new Error(`${{post.status}} ${{await post.text()}}`);
  for (;;) {{
    const got = await fetch('/api/invocations/' + encodeURIComponent(id));
    if (!got.ok) throw new Error(`${{got.status}} ${{await got.text()}}`);
    const state = await got.json();
    if (state.status === 'failed') throw new Error(state.error?.message || 'run failed');
    if (state.status === 'completed') {{
      const messages = state.output?.messages || [];
      const halt = messages.filter(m => m.type === 'ai' && m.additional_kwargs?.apx_answer === true);
      return (halt.length ? halt : messages.filter(m => m.type === 'ai')).map(m =>
        typeof m.content === 'string' ? m.content : (m.content || []).filter(p => p.type === 'text').map(p => p.text).join('')
      ).filter(Boolean).join('\\n');
    }}
    await new Promise(done => setTimeout(done, 1500));
  }}
}}

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
      ? `<button class="run-btn" onclick="runCase(${{i}})" title="Run" aria-label="Run">&#9654;</button>` : '';
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
      ${{r.trace_id ? `<div class="case-row"><span class="label">Source trace</span><a href="/_apx/traces/${{encodeURIComponent(r.trace_id)}}" target="_blank" style="color:var(--apx-accent);font-size:11px">${{r.trace_id.split('/').pop().slice(0,16)}}…</a></div>` : ''}}
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
    if (NATIVE) text = await runNative(r.question);
    else {{
    const resp = await fetch('/invocations', {{
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
}});

// ── Judge Alignment ──────────────────────────────────────────────────────────
function renderAlignHistory(runs) {{
  const el = document.getElementById('la-history');
  if (!el) return;
  if (!runs || !runs.length) {{
    el.innerHTML = '<div style="font-size:12px;color:var(--muted)">No alignments yet.</div>';
    return;
  }}
  const n = runs.length;
  el.innerHTML =
    `<div style="font-size:12px;color:var(--muted);margin:0 0 8px">Judge aligned ${{n}} time${{n === 1 ? '' : 's'}}</div>` +
    runs.map((run) => {{
      const when = run.start_time ? esc(String(run.start_time).replace('T', ' ').slice(0, 19)) : 'unknown time';
      const judge = esc(run.judge_name || '');
      const gs = (run.guidelines || []).map((g, i) => `${{i + 1}}. ${{esc(g)}}`).join('<br>');
      const empty = gs ? `<small style="color:var(--apx-muted)">${{gs}}</small>` : '<small style="color:var(--apx-muted)">No guidelines stored.</small>';
      return `<div style="border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:6px;background:var(--panel)">
        <div style="font-size:11px;color:var(--muted);margin-bottom:4px">${{when}}${{judge ? ' · ' + judge : ''}} · ${{run.trace_count || 0}} traces</div>
        ${{empty}}
      </div>`;
    }}).join('');
}}

async function loadAlignHistory() {{
  const el = document.getElementById('la-history');
  const judge = document.getElementById('la-judge') ? document.getElementById('la-judge').value.trim() : '';
  const qs = judge ? ('?judge_name=' + encodeURIComponent(judge)) : '';
  try {{
    const r = await fetch('/_apx/eval/label-history' + qs);
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || r.statusText);
    renderAlignHistory(d.runs || []);
  }} catch (e) {{
    if (el) el.innerHTML = '<div style="font-size:12px;color:var(--apx-muted)">Could not load alignment history: ' + esc(e.message) + '</div>';
  }}
}}

async function labelAlign() {{
  const judge = document.getElementById('la-judge').value.trim();
  if (!judge) {{ document.getElementById('la-status').textContent = 'Enter a judge name (e.g. quality).'; return; }}
  document.getElementById('la-align-btn').disabled = true;
  document.getElementById('la-status').textContent = 'Reading your ratings and running MemAlign… (may take a minute)';
  try {{
    const r = await fetch('/_apx/eval/label-align', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{judge_name: judge}}),
    }});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    const gs = (d.guidelines || []).map((g, i) => `${{i+1}}. ${{esc(g)}}`).join('<br>');
    document.getElementById('la-status').innerHTML =
      `✓ Aligned on ${{d.trace_count}} traces — <strong>${{(d.guidelines || []).length}} guidelines distilled</strong><br><small style="color:var(--apx-muted)">${{gs}}</small>`;
    await loadAlignHistory();
  }} catch(e) {{
    document.getElementById('la-status').textContent = 'Error: ' + e.message;
  }} finally {{
    document.getElementById('la-align-btn').disabled = false;
  }}
}}
document.getElementById('la-judge').addEventListener('change', loadAlignHistory);
loadAlignHistory();
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

    parts.append(
        '<div id="latency-spark" hidden>'
        '<div class="landing-label">Latency</div>'
        '<div class="latency-spark-card">'
        '<div id="latency-spark-empty" class="latency-spark-empty" hidden>No traces yet</div>'
        '<div id="latency-spark-body" hidden>'
        '<svg id="latency-spark-svg" viewBox="0 0 240 36" width="240" height="36" '
        'aria-hidden="true" preserveAspectRatio="none">'
        '<polyline id="latency-spark-line" fill="none" stroke="var(--apx-accent)" '
        'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" points=""/>'
        '</svg>'
        '<div class="data-pills">'
        '<span class="data-pill" id="latency-p50">p50 —</span>'
        '<span class="data-pill" id="latency-p95">p95 —</span>'
        '</div>'
        '</div>'
        '</div>'
        '</div>'
    )

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


def _render_agent_ui(ctx: AgentContext | None, *, embed: bool = False, native: bool = False) -> str:
    """Return a self-contained HTML page for interactively testing the agent."""
    from ._ui_table import TABLE_CSS, TABLE_JS

    agent_name = ctx.config.name if ctx else "Agent"
    agent_desc = ctx.config.description if ctx else ""
    body_class = ' class="apx-embed"' if embed else ""
    trace_active = ' class="active"' if embed else ""
    events_active = "" if embed else ' class="active"'
    trace_panel_class = "tab-panel active" if embed else "tab-panel"
    events_panel_class = "tab-panel" if embed else "tab-panel active"
    not_configured = ctx is None
    setup_banner = """
<div id="setup-banner">
  <strong>⚠ Agent not configured</strong><br>
  Declare an agent in <code>agent.py</code> and create
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
                '<div id="setup-banner" style="background:var(--apx-panel-2);border-color:var(--apx-panel-2);color:var(--apx-warn)">'
                '<strong>👋 First time here?</strong> '
                '<a href="/_apx/setup" target="_top" style="color:var(--apx-warn);text-decoration:underline">Open Setup</a> '
                'to connect your data and generate tools automatically.'
                '</div>'
            )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{agent_name} — APX Dev</title>
{apx_theme_style()}
<style>
{{APX_TABLE_CSS}}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: var(--apx-bg); color: var(--apx-text); height: 100vh; display: flex; flex-direction: column; }}

  /* --- Header --- */
  header {{ padding: 14px 24px; background: var(--apx-panel); border-bottom: 1px solid var(--apx-border);
            display: flex; align-items: center; gap: 14px; flex-shrink: 0; }}
  .badge {{ background: var(--apx-panel-2); color: var(--apx-accent); font-size: 11px; font-weight: 600;
            padding: 3px 10px; border-radius: 4px; letter-spacing: .5px; text-transform: uppercase; }}
  header h1 {{ font-size: 17px; font-weight: 600; color: var(--apx-on-accent); }}
  header .desc {{ font-size: 13px; color: var(--apx-muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  nav {{ display: flex; gap: 4px; }}
  nav a {{ font-size: 12px; color: var(--apx-muted); text-decoration: none; padding: 5px 12px;
           border-radius: 6px; border: 1px solid transparent; }}
  nav a:hover {{ color: var(--apx-muted); border-color: var(--apx-border); }}
  nav a.active {{ color: var(--apx-accent); background: var(--apx-panel-2); border-color: var(--apx-panel-2); }}

  /* --- Main layout: chat left, tools right --- */
  .main {{ flex: 1; display: flex; overflow: hidden; }}

  /* --- Chat panel (left/center) --- */
  .chat-panel {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
  #chat {{ flex: 1; overflow-y: auto; padding: 28px 32px; display: flex; flex-direction: column; gap: 16px; }}
  .msg {{ max-width: 720px; line-height: 1.6; font-size: 15px; }}
  .msg.user {{ align-self: flex-end; background: var(--apx-panel-2); color: var(--apx-text);
               padding: 12px 18px; border-radius: 16px 16px 4px 16px; }}
  .msg.assistant {{ align-self: flex-start; color: var(--apx-text); white-space: pre-wrap; }}
  .msg.assistant.streaming::after {{ content: "▋"; animation: blink .7s step-end infinite; }}
  .msg.system {{ align-self: center; font-size: 13px; color: var(--apx-muted); font-style: italic; padding: 20px 0; }}
  /* --- Rendered assistant markdown (tables, code, lists, headings) --- */
  .msg.assistant table {{ display: block; max-width: 100%; overflow-x: auto; border-collapse: collapse; margin: 8px 0; font-size: 12px; }}
  .msg.assistant th, .msg.assistant td {{ border: 1px solid var(--apx-border); padding: 5px 9px; text-align: left; }}
  .msg.assistant th {{ background: var(--apx-panel); color: var(--apx-text); font-weight: 600; }}
  .msg.assistant tr:nth-child(even) td {{ background: var(--apx-bg); }}
  .msg.assistant pre {{ background: var(--apx-panel); border: 1px solid var(--apx-border); border-radius: 6px; padding: 10px; overflow-x: auto; font-size: 12px; }}
  .msg.assistant code {{ background: var(--apx-panel); border-radius: 4px; padding: 1px 5px; font-size: 12px; font-family: ui-monospace, monospace; }}
  .msg.assistant pre code {{ background: none; padding: 0; }}
  .msg.assistant h1, .msg.assistant h2, .msg.assistant h3 {{ margin: 10px 0 6px; line-height: 1.3; }}
  .msg.assistant ul, .msg.assistant ol {{ margin: 6px 0 6px 20px; }}
  .msg.assistant li {{ margin: 2px 0; }}
  .msg.assistant a {{ color: var(--apx-accent); }}
  .msg.assistant p {{ margin: 6px 0; }}
  .msg.assistant > :first-child {{ margin-top: 0; }}
  .msg.assistant > :last-child {{ margin-bottom: 0; }}
  .trace-inline {{ align-self: flex-start; display: flex; align-items: center; gap: 8px;
                   margin: -8px 0 4px; font-size: 11px; color: var(--apx-muted); }}
  .trace-inline button, .trace-inline a {{ background: transparent; border: 1px solid var(--apx-panel-2);
                                           border-radius: 5px; color: var(--apx-accent); cursor: pointer;
                                           font: inherit; padding: 3px 8px; text-decoration: none; }}
  .trace-inline button:hover, .trace-inline a:hover {{ background: var(--apx-panel-2); }}
  .trace-inline code {{ color: var(--apx-text); font-family: ui-monospace, monospace; }}
  @keyframes blink {{ 50% {{ opacity: 0; }} }}

  /* Inline tool call pills */
  .tool-pills {{ align-self: flex-start; display: flex; flex-wrap: wrap; gap: 8px; margin: 2px 0 4px; }}
  .tool-pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 6px 14px;
                 border-radius: 8px; font-size: 13px; font-family: monospace; cursor: pointer;
                 border: 1px solid var(--apx-border); transition: all .15s; }}
  .tool-pill:hover {{ border-color: var(--apx-muted); transform: translateY(-1px); }}
  .tool-pill.call {{ background: var(--apx-panel-2); color: var(--apx-accent); border-color: var(--apx-panel-2); }}
  .tool-pill.result {{ background: var(--apx-panel-2); color: var(--apx-ok); border-color: var(--apx-panel-2); }}
  .tool-pill.error {{ background: var(--apx-panel-2); color: var(--apx-err); border-color: var(--apx-panel-2); }}
  .tool-pill .icon {{ font-size: 12px; }}
  .tool-pill .ms {{ font-size: 11px; color: var(--apx-muted); margin-left: 4px; }}

  /* Inline thinking-steps (live tool rows above the answer) */
  .inline-steps {{ align-self: flex-start; width: 100%; }}
  .inline-step {{ background: var(--apx-panel); border: 1px solid var(--apx-border); border-radius: 8px; margin: 6px 0; padding: 0; max-width: 680px; }}
  .inline-step.error {{ border-color: var(--apx-panel-2); }}
  .inline-step-head {{ display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; font-size: 12.5px; }}
  .inline-step-head .step-icon {{ color: var(--apx-accent); }}
  .inline-step.error .step-icon {{ color: var(--apx-err); }}
  .inline-step-head .step-name {{ color: var(--apx-text); font-family: ui-monospace, monospace; }}
  .inline-step-head .step-label {{ color: var(--apx-muted); margin-left: auto; font-size: 11px; }}
  .inline-step-head .step-caret {{ color: var(--apx-muted); font-size: 10px; transition: transform .12s; }}
  .inline-step.open .step-caret {{ transform: rotate(90deg); }}
  .inline-step-detail {{ display: none; margin: 0; padding: 0 12px 10px; }}
  .inline-step.open .inline-step-detail {{ display: block; }}
  .step-detail-label {{ font-size: 10px; color: var(--apx-muted); text-transform: uppercase; letter-spacing: .5px; margin: 8px 0 3px; }}
  .step-detail-pre {{ margin: 0; color: var(--apx-muted); font-size: 11px; white-space: pre-wrap;
                      font-family: ui-monospace, monospace; }}
  .step-sql {{ display: block; color: var(--apx-muted); font-size: 11px; white-space: pre-wrap;
               font-family: ui-monospace, monospace; }}
  .resp-table-wrap {{ overflow-x: auto; max-width: 100%; }}
  .resp-table {{ border-collapse: collapse; font-size: 11px; width: 100%; margin-bottom: 4px; }}
  .resp-table th {{ color: var(--apx-accent); text-align: left; padding: 3px 8px;
                    border-bottom: 1px solid var(--apx-border); font-weight: 600; white-space: nowrap; }}
  .resp-table td {{ color: var(--apx-text); padding: 3px 8px; border-bottom: 1px solid var(--apx-border); }}
  .resp-table tr:last-child td {{ border-bottom: none; }}
  .resp-meta {{ font-size: 10px; color: var(--apx-muted); margin-top: 4px; }}
  .trunc-note {{ color: var(--apx-warn); }}

  /* Input area */
  .input-bar {{ display: flex; gap: 10px; padding: 16px 24px; background: var(--apx-panel);
                 border-top: 1px solid var(--apx-border); flex-shrink: 0; }}
  .input-bar textarea {{ flex: 1; background: var(--apx-panel); border: 1px solid var(--apx-border); color: var(--apx-text);
                          border-radius: 10px; padding: 12px 16px; font-size: 15px; resize: none;
                          font-family: inherit; line-height: 1.5; outline: none; max-height: 160px; }}
  .input-bar textarea:focus {{ border-color: var(--apx-accent); }}
  .input-bar button {{ background: var(--apx-accent); color: var(--apx-on-accent); border: none; border-radius: 10px;
                        padding: 12px 20px; font-size: 14px; cursor: pointer; align-self: flex-end;
                        white-space: nowrap; font-weight: 500; }}
  .input-bar button:hover {{ background: var(--apx-accent); }}
  .input-bar button:disabled {{ background: var(--apx-panel-2); color: var(--apx-muted); cursor: not-allowed; }}

  /* --- Right panel: tools & events --- */
  .resize-handle {{ width: 5px; cursor: col-resize; background: transparent; flex-shrink: 0; }}
  .resize-handle:hover {{ background: var(--apx-accent); }}
  .right-panel {{ width: 420px; min-width: 280px; max-width: 700px; background: var(--apx-bg);
                   border-left: 1px solid var(--apx-border); display: flex; flex-direction: column; flex-shrink: 0; }}
  .panel-tabs {{ display: flex; border-bottom: 1px solid var(--apx-border); flex-shrink: 0; }}
  .panel-tabs button {{ flex: 1; background: none; border: none; color: var(--apx-muted); font-size: 13px; font-weight: 500;
                          padding: 12px 0; cursor: pointer; border-bottom: 2px solid transparent; transition: all .15s; }}
  .panel-tabs button:hover {{ color: var(--apx-muted); }}
  .panel-tabs button.active {{ color: var(--apx-accent); border-bottom-color: var(--apx-accent); }}
  .panel-content {{ flex: 1; overflow-y: auto; }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  /* Trace tab uses flex column so trace-body can scroll independently */
  #tab-trace.active {{ display: flex; flex-direction: column; height: 100%; }}
  #tab-history.active {{ display: flex; flex-direction: column; height: 100%; }}
  #tab-tools {{ min-height: 0; }}
  #tab-tools.active {{ display: flex; flex-direction: column; height: 100%; }}
  .tools-embed-head {{ padding: 10px 12px; border-bottom: 1px solid var(--apx-border); display:flex; align-items:center; justify-content:space-between; gap:10px; flex-shrink:0; font-size:11px; color:var(--apx-muted); }}
  .tools-embed-head a {{ color:var(--apx-accent); text-decoration:none; white-space:nowrap; }}
  .tools-embed-head a:hover {{ text-decoration:underline; }}
  #tools-embed {{ flex: 1; min-height: 0; display: flex; }}
  #tools-embed iframe {{ width: 100%; height: 100%; border: 0; background: var(--apx-bg); }}
  .conv-toolbar {{ padding: 8px 12px; border-bottom: 1px solid var(--apx-border); display: flex; align-items: center; justify-content: flex-end; flex-shrink: 0; }}
  .conv-new-btn {{ background: transparent; color: var(--apx-accent); border: 1px solid var(--apx-panel-2); border-radius: 5px; padding: 4px 10px; font-size: 12px; cursor: pointer; }}
  .conv-new-btn:hover {{ background: var(--apx-panel-2); }}
  .conv-list {{ overflow-y: auto; flex: 1; }}
  .conv-item {{ padding: 10px 12px; cursor: pointer; border-bottom: 1px solid var(--apx-panel); transition: background .1s; }}
  .conv-item:hover {{ background: var(--apx-panel); }}
  .conv-item.active {{ background: var(--apx-panel-2); border-left: 2px solid var(--apx-accent); padding-left: 10px; }}
  .conv-title {{ font-size: 13px; color: var(--apx-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .conv-item.active .conv-title {{ color: var(--apx-accent); }}
  .conv-ts {{ font-size: 11px; color: var(--apx-muted); margin-top: 2px; }}

  /* Trace tab — live span bubbles, mirrors /_apx/traces/{{id}} detail view */
  .span-step {{ position: relative; padding-left: 22px; margin-bottom: 4px; }}
  .span-step .step-line {{ position: absolute; left: 6px; top: 18px; bottom: -4px; width: 1px; background: var(--apx-border); }}
  .span-step:last-child .step-line {{ display: none; }}
  .span-step .step-dot {{ position: absolute; left: 1px; top: 5px; width: 11px; height: 11px; border-radius: 50%; }}
  .span-step.in-progress .step-dot {{ animation: span-pulse 1.2s ease-in-out infinite; }}
  @keyframes span-pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
  .span-step .step-content {{ padding-bottom: 10px; }}
  .span-step .step-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; font-size: 12px; }}
  .span-step .who {{ font-weight: 600; }}
  .span-step .dur {{ font-size: 10px; color: var(--apx-muted); font-family: monospace; }}
  .span-step .span-bubble {{ padding: 6px 10px; border-radius: 8px; font-size: 12px; line-height: 1.5;
                              white-space: pre-wrap; word-break: break-word; max-width: 100%; }}
  .span-step .span-bubble + .span-bubble {{ margin-top: 4px; }}
  .span-bubble.caller {{ background: var(--apx-panel-2); color: var(--apx-muted); border: 1px solid var(--apx-border); }}
  .span-bubble.agent-ask {{ background: var(--apx-panel-2); color: var(--apx-text); border: 1px solid var(--apx-panel-2); }}
  .span-bubble.llm-reply {{ background: var(--apx-panel-2); color: var(--apx-text); border: 1px solid var(--apx-panel-2); }}
  .span-bubble.tool-in {{ background: var(--apx-panel-2); color: var(--apx-text); border: 1px solid var(--apx-panel-2); }}
  .span-bubble.tool-out {{ background: var(--apx-panel-2); color: var(--apx-text); border: 1px solid var(--apx-panel-2); }}
  .span-bubble.agent-reply {{ background: var(--apx-panel-2); color: var(--apx-text); border: 1px solid var(--apx-border); }}
  .span-bubble.response {{ background: var(--apx-panel-2); color: var(--apx-text); border: 1px solid var(--apx-panel-2); }}
  .span-bubble.error-msg {{ background: var(--apx-panel-2); color: var(--apx-err); border: 1px solid var(--apx-panel-2); }}

  /* Events list */
  .event {{ display: flex; align-items: flex-start; gap: 10px; padding: 10px 16px; border-bottom: 1px solid var(--apx-panel);
            cursor: pointer; font-size: 13px; line-height: 1.4; transition: background .1s; }}
  .event:hover {{ background: var(--apx-panel); }}
  .event.selected {{ background: var(--apx-panel-2); }}
  .events-filter {{ display: flex; gap: 4px; padding: 8px 16px; border-bottom: 1px solid var(--apx-panel); }}
  .events-filter button {{ background: none; border: 1px solid var(--apx-border); border-radius: 12px; color: var(--apx-muted);
                           font-size: 12px; padding: 2px 10px; cursor: pointer; }}
  .events-filter button.active {{ background: var(--apx-panel-2); color: var(--apx-text); }}
  .event-num {{ color: var(--apx-muted); font-size: 12px; font-family: monospace; min-width: 26px; text-align: right; flex-shrink: 0; }}
  .event-icon {{ flex-shrink: 0; font-size: 14px; }}
  .event-body {{ flex: 1; min-width: 0; }}
  .event-title {{ color: var(--apx-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .event-sub {{ color: var(--apx-muted); font-size: 12px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .event.tool-call .event-title {{ color: var(--apx-accent); }}
  .event.tool-result .event-title {{ color: var(--apx-ok); }}
  .event.tool-error .event-title {{ color: var(--apx-err); }}
  /* Tool-call group: call + response paired in one collapsible block. */
  .event.tool-group {{ flex-direction: column; align-items: stretch; gap: 0; padding: 0; cursor: default; }}
  .event.tool-group:hover {{ background: transparent; }}
  .tg-head {{ display: flex; align-items: center; gap: 10px; padding: 10px 16px; cursor: pointer; }}
  .tg-head:hover {{ background: var(--apx-panel); }}
  .tg-name {{ flex: 1; color: var(--apx-accent); font-family: ui-monospace, monospace; font-size: 13px;
              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .tg-hint {{ color: var(--apx-muted); font-size: 11px; white-space: nowrap; }}
  .tg-caret {{ color: var(--apx-muted); font-size: 10px; flex-shrink: 0; transition: transform .12s; }}
  .event.tool-group:not(.open) .tg-caret {{ transform: rotate(-90deg); }}
  .tg-body {{ display: none; flex-direction: column; gap: 1px; padding: 0 16px 8px 52px; }}
  .event.tool-group.open .tg-body {{ display: flex; }}
  .tg-part {{ display: flex; gap: 10px; font-size: 12px; line-height: 1.5; cursor: pointer;
              padding: 3px 6px; border-radius: 4px; }}
  .tg-part:hover {{ background: var(--apx-border); }}
  .tg-label {{ flex: none; min-width: 62px; color: var(--apx-muted); text-transform: uppercase;
               font-size: 10px; letter-spacing: .4px; font-weight: 600; padding-top: 1px; }}
  .tg-part.err .tg-label {{ color: var(--apx-err); }}
  .tg-val {{ color: var(--apx-muted); font-family: ui-monospace, monospace; min-width: 0;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

  /* Tool test panel */
  .tool-card {{ border-bottom: 1px solid var(--apx-border); }}
  .tool-card-header {{ padding: 12px 16px; cursor: pointer; display: flex; align-items: center; gap: 10px; }}
  .tool-card-header:hover {{ background: var(--apx-panel); }}
  .tool-card-header .arrow {{ color: var(--apx-muted); font-size: 10px; transition: transform .15s; }}
  .tool-card.open .arrow {{ transform: rotate(90deg); }}
  .tool-card-header .tname {{ color: var(--apx-accent); font-size: 14px; font-weight: 600; font-family: monospace; }}
  .tool-card-header .tbadge {{ font-size: 10px; color: var(--apx-muted); background: var(--apx-border); padding: 2px 6px;
                                border-radius: 3px; margin-left: auto; }}
  .btn-delete-tool {{ margin-left: auto; background: none; border: none; color: var(--apx-border);
                      cursor: pointer; font-size: 12px; padding: 2px 6px; border-radius: 4px; line-height: 1; }}
  .btn-delete-tool:hover {{ color: var(--apx-err); background: var(--apx-panel-2); }}
  .tool-card-body {{ display: none; padding: 0 16px 16px; }}
  .tool-card.open .tool-card-body {{ display: block; }}
  .tool-card-body .tdesc {{ font-size: 12px; color: var(--apx-muted); margin-bottom: 12px; line-height: 1.5; }}
  .tool-card-body label {{ display: block; font-size: 12px; color: var(--apx-muted); margin-bottom: 4px; margin-top: 10px; }}
  .tool-card-body label:first-of-type {{ margin-top: 0; }}
  .tool-card-body input {{ width: 100%; background: var(--apx-panel); border: 1px solid var(--apx-border); color: var(--apx-text);
                            border-radius: 6px; padding: 8px 12px; font-size: 13px; font-family: monospace;
                            outline: none; }}
  .tool-card-body input:focus {{ border-color: var(--apx-accent); }}
  .tool-card-body input::placeholder {{ color: var(--apx-muted); }}
  .tool-run {{ margin-top: 12px; display: flex; gap: 8px; align-items: center; }}
  .tool-run button {{ background: var(--apx-panel-2); color: var(--apx-ok); border: 1px solid var(--apx-panel-2); border-radius: 6px;
                       padding: 7px 16px; font-size: 12px; font-weight: 600; cursor: pointer; }}
  .tool-run button:hover {{ background: var(--apx-panel-2); }}
  .tool-run button:disabled {{ opacity: .5; cursor: not-allowed; }}
  .tool-run .run-ms {{ font-size: 11px; color: var(--apx-muted); }}
  .tool-result-box {{ margin-top: 10px; background: var(--apx-panel); border: 1px solid var(--apx-border); border-radius: 6px;
                       padding: 10px 12px; font-family: monospace; font-size: 12px; color: var(--apx-muted);
                       white-space: pre-wrap; word-break: break-all; max-height: 240px; overflow-y: auto; line-height: 1.5; }}
  .tool-result-box.err {{ color: var(--apx-err); border-color: var(--apx-panel-2); }}

  /* Detail overlay */
  .detail-panel {{ border-top: 1px solid var(--apx-border); max-height: 40%; overflow-y: auto;
                    background: var(--apx-panel); flex-shrink: 0; display: none; }}
  .detail-panel.open {{ display: block; }}
  .detail-header {{ padding: 10px 16px; font-size: 12px; color: var(--apx-muted); display: flex; align-items: center;
                     border-bottom: 1px solid var(--apx-border); position: sticky; top: 0; background: var(--apx-panel); }}
  .detail-header span {{ flex: 1; }}
  .detail-close {{ background: none; border: none; color: var(--apx-muted); cursor: pointer; font-size: 16px; padding: 0 4px; }}
  .detail-close:hover {{ color: var(--apx-on-accent); }}
  .detail-body {{ padding: 12px 16px; }}
  .detail-body pre {{ font-family: monospace; font-size: 12px; color: var(--apx-muted); white-space: pre-wrap;
                       word-break: break-all; line-height: 1.6; }}
  .detail-body .label {{ font-size: 10px; color: var(--apx-muted); text-transform: uppercase; letter-spacing: .5px; margin: 10px 0 4px; }}
  .detail-body .label:first-child {{ margin-top: 0; }}

  /* MCP bar */
  .mcp-bar {{ padding: 8px 16px; font-size: 12px; color: var(--apx-muted); border-bottom: 1px solid var(--apx-border);
               display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
  .mcp-bar strong {{ color: var(--apx-ok); font-size: 10px; letter-spacing: .5px; text-transform: uppercase; }}
  .mcp-bar code {{ background: var(--apx-panel-2); color: var(--apx-ok); padding: 2px 8px; border-radius: 3px;
                    font-family: monospace; font-size: 11px; }}
  .mcp-bar .cbtn {{ background: none; border: 1px solid var(--apx-border); color: var(--apx-muted); border-radius: 3px;
                     padding: 1px 8px; font-size: 11px; cursor: pointer; }}
  .mcp-bar .cbtn:hover {{ border-color: var(--apx-ok); color: var(--apx-ok); }}

  /* Tooltip */
  .tooltip {{ display: none; position: fixed; background: var(--apx-border); border: 1px solid var(--apx-border);
              border-radius: 8px; padding: 10px 14px; font-family: monospace; font-size: 12px;
              color: var(--apx-muted); max-width: 500px; max-height: 320px; overflow-y: auto;
              white-space: pre-wrap; word-break: break-all; z-index: 100; line-height: 1.5;
              box-shadow: 0 8px 32px rgba(0,0,0,.6); pointer-events: none; }}
  .tooltip.show {{ display: block; }}

  /* Setup banner */
  #setup-banner {{ background: var(--apx-panel-2); border-bottom: 1px solid var(--apx-panel-2); color: var(--apx-warn);
                   padding: 12px 24px; font-size: 13px; line-height: 1.6; flex-shrink: 0; }}
  #setup-banner code {{ background: var(--apx-panel-2); padding: 1px 5px; border-radius: 3px; font-family: monospace; font-size: 12px; }}
  .empty-state {{ padding: 24px; color: var(--apx-muted); font-size: 13px; text-align: center; }}

  /* --- Landing (empty-chat) --- */
  #landing {{ padding: 28px 22px; max-width: 680px; }}
  .landing-hi {{ font-size: 19px; font-weight: 600; color: var(--apx-on-accent); margin-bottom: 4px; }}
  .landing-sub {{ font-size: 13px; color: var(--apx-muted); margin-bottom: 18px; line-height: 1.4; }}
  .data-card {{ background: var(--apx-panel); border: 1px solid var(--apx-border); border-radius: 10px;
                padding: 12px 14px; margin: 10px 0; max-width: 680px; }}
  .data-card-head {{ font-size: 12.5px; color: var(--apx-muted); margin-bottom: 8px; }}
  .data-pills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .data-pill {{ background: var(--apx-panel); border: 1px solid var(--apx-border); border-radius: 5px;
                padding: 3px 9px; font-size: 11.5px; color: var(--apx-text);
                font-family: ui-monospace, monospace; white-space: nowrap; }}
  .data-pill-more {{ color: var(--apx-muted); border-color: var(--apx-border); }}
  .landing-label {{ font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--apx-muted); margin: 16px 0 8px; }}
  .cap-cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .cap-card {{ background: var(--apx-panel); border: 1px solid var(--apx-panel); border-radius: 8px; padding: 11px 13px; cursor: pointer; }}
  .cap-card:hover {{ border-color: var(--apx-border); }}
  .cap-name {{ color: var(--apx-text); font-size: 12.5px; font-family: ui-monospace, monospace; }}
  .cap-desc {{ color: var(--apx-muted); font-size: 11px; margin-top: 3px; line-height: 1.35; }}
  .cap-params {{ display: none; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--apx-border);
                 color: var(--apx-muted); font-size: 10.5px; white-space: pre-wrap; }}
  .cap-card.open .cap-params {{ display: block; }}
  .cap-card.open {{ border-color: var(--apx-border); }}
  .cap-mem-link {{ color: var(--apx-accent); font-size: 10px; text-decoration: none; margin-left: 6px; vertical-align: middle; opacity: .7; }}
  .cap-mem-link:hover {{ opacity: 1; }}
  .cap-mem-preview {{ margin-top: 8px; display: flex; flex-direction: column; gap: 3px; }}
  .cap-mem-preview:empty {{ display: none; }}
  .cap-mem-row {{ font-size: 11.5px; color: var(--apx-text); line-height: 1.4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .cap-mem-row-ts {{ font-size: 10px; color: var(--apx-muted); font-family: ui-monospace, monospace; margin-left: 4px; }}
  .cap-mem-ops {{ margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--apx-border); display: flex; flex-direction: column; gap: 4px; }}
  .cap-mem-op {{ display: flex; flex-direction: column; gap: 1px; }}
  .cap-mem-name {{ color: var(--apx-text); font-size: 11px; font-family: ui-monospace, monospace; }}
  .cap-mem-desc {{ color: var(--apx-muted); font-size: 10.5px; line-height: 1.35; }}
  .starter-chip {{ display: inline-block; background: var(--apx-panel); border: 1px solid var(--apx-border); color: var(--apx-text);
                   border-radius: 8px; padding: 9px 16px; font-size: 13px; margin: 0 8px 8px 0;
                   cursor: pointer; text-align: left; transition: background 0.12s, border-color 0.12s; }}
  .starter-chip:hover {{ background: var(--apx-panel-2); border-color: var(--apx-border); }}
  .starter-chip:active {{ background: var(--apx-panel-2); }}
  .workflow-chip {{ display: inline-flex; flex-direction: column; gap: 3px; min-width: 220px; }}
  .workflow-title {{ color: var(--apx-text); font-weight: 600; }}
  .workflow-purpose {{ color: var(--apx-muted); font-size: 11px; }}
  .workflow-question {{ color: var(--apx-text); font-size: 12px; }}
  #latency-spark[hidden],
  #latency-spark-empty[hidden],
  #latency-spark-body[hidden] {{ display: none; }}
  .latency-spark-card {{ background: var(--apx-panel); border: 1px solid var(--apx-border); border-radius: 10px;
                padding: 12px 14px; margin: 10px 0; max-width: 680px;
                display: flex; flex-direction: column; gap: 8px; }}
  .latency-spark-empty {{ font-size: 12px; color: var(--apx-muted); }}
  #latency-spark-svg {{ display: block; width: 100%; max-width: 240px; height: 36px; }}
  #latency-p50, #latency-p95 {{ color: var(--apx-muted); }}

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
    border-top: 1px solid var(--apx-border);
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
      <span id="copy-ok" style="display:none;color:var(--apx-ok)">✓</span>
      <span style="color:var(--apx-border);margin:0 4px">·</span>
      <span style="color:var(--apx-muted);font-size:11px">SSE:</span>
      <button class="cbtn" onclick="copyMcpSseUrl()" title="Copy /mcp/sse (Claude Desktop, Cursor)">/sse</button>
      <span id="copy-sse-ok" style="display:none;color:var(--apx-ok)">✓</span>
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
      <div id="tab-tools" class="tab-panel">
        <div class="tools-embed-head">
          <span>Tool logic — source read from the running process</span>
          <a href="/_apx/tools" target="_blank" rel="noopener">Open full page ↗</a>
        </div>
        <div id="tools-embed"></div>
      </div>
      <div id="tab-trace" class="{trace_panel_class}">
        <div id="trace-header" style="padding:8px 12px;border-bottom:1px solid var(--apx-border);font-size:11px;color:var(--apx-muted);display:flex;justify-content:space-between;align-items:center">
          <span id="trace-status">No trace yet — send a message</span>
          <a id="trace-link" href="#" target="_blank" style="display:none;color:var(--apx-accent);text-decoration:none;font-size:11px">open full →</a>
        </div>
        <div id="trace-body" style="overflow-y:auto;flex:1;padding:12px"></div>
      </div>
      <div id="tab-events" class="{events_panel_class}">
        <div id="events-filter" class="events-filter"><button type="button" data-filter="all" class="active">All</button><button type="button" data-filter="messages">Messages</button><button type="button" data-filter="tools">Tools</button><button type="button" data-filter="steps">Steps</button></div>
        <div id="events-list" class="empty-state">Send a message to see events</div>
      </div>
      <div id="tab-eval" class="tab-panel">
        <div id="eval-toolbar" style="display:flex;gap:6px;align-items:center;padding:10px 12px;border-bottom:1px solid var(--apx-border);flex-shrink:0">
          <button id="eval-run-all" style="background:var(--apx-panel-2);color:var(--apx-accent);border:1px solid var(--apx-border);border-radius:5px;padding:5px 12px;font-size:12px;cursor:pointer">▶ Run All</button>
          <button id="eval-reset" style="background:transparent;color:var(--apx-muted);border:1px solid var(--apx-border);border-radius:5px;padding:5px 10px;font-size:12px;cursor:pointer">↺ Reset</button>
          <span id="eval-status" style="font-size:11px;color:var(--apx-muted);margin-left:4px"></span>
        </div>
        <div id="eval-progress" style="height:2px;background:var(--apx-border)"><div id="eval-progress-fill" style="height:100%;background:var(--apx-accent);width:0%;transition:width .3s"></div></div>
        <div id="eval-cases" style="overflow-y:auto;flex:1;padding:6px 0">
          <div style="color:var(--apx-muted);font-size:12px;padding:20px 12px">Click Eval tab to load test cases</div>
        </div>
        <div id="eval-add" style="padding:10px 12px;border-top:1px solid var(--apx-border)">
          <textarea id="eval-add-q" placeholder="Add a test question…" rows="2" style="width:100%;background:var(--apx-panel);border:1px solid var(--apx-border);color:var(--apx-muted);border-radius:5px;padding:6px 8px;font-size:12px;resize:none;margin-bottom:6px"></textarea>
          <button id="eval-add-btn" style="background:transparent;color:var(--apx-muted);border:1px solid var(--apx-border);border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer">+ Add</button>
        </div>
        <div style="padding:10px 12px;border-top:1px solid var(--apx-border)">
          <div style="font-size:10px;font-weight:700;color:var(--apx-muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px">Judge Alignment</div>
          <div style="display:flex;gap:6px;align-items:center">
            <input id="eval-judge-name" placeholder="Judge name (e.g. quality)" style="flex:1;background:var(--apx-panel);border:1px solid var(--apx-border);color:var(--apx-muted);border-radius:5px;padding:5px 8px;font-size:11px" />
            <button id="eval-align-btn" onclick="chatLabelAlign()" style="background:var(--apx-panel-2);color:var(--apx-accent);border:1px solid var(--apx-border);border-radius:5px;padding:5px 10px;font-size:11px;cursor:pointer;white-space:nowrap">Run alignment</button>
          </div>
          <div id="eval-align-status" style="font-size:11px;color:var(--apx-muted);margin-top:4px;min-height:14px"></div>
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

<script src="/_apx/vendor/legacy-chat-prelude.js"></script>
<script src="/_apx/vendor/legacy-chat-history.js"></script>
<script>
{{APX_TABLE_JS}}
</script>
<script>window.__APX_NATIVE__ = {str(native).lower()};</script>
<script src="/_apx/vendor/legacy-chat-app.js"></script>
{_deploy_overlay_html()}
{{APX_NATIVE_CHAT}}
</body>
</html>"""
    from ._ui_native_chat import native_chat_script

    if native:
        from ._ui_root_chat import _inline_vendor

        page = page.replace('<script src="/_apx/vendor/marked.min.js"></script>', _inline_vendor())
        page = page.replace('<script src="/_apx/vendor/purify.min.js"></script>', '')
        # Native pages remain self-contained; keep the history script at its original
        # parser position so its classic-script globals share the page's state.
        from pathlib import Path

        vendor = Path(__file__).parent / "_static" / "vendor"
        # Native pages stay self-contained for the legacy chat scripts: inline the
        # same files, in the same order, so classic-script globals still share state.
        # native-chat.js stays a real script request (the dev vendor route serves it).
        for filename in ("legacy-chat-prelude.js", "legacy-chat-history.js", "legacy-chat-app.js"):
            tag = f'<script src="/_apx/vendor/{filename}"></script>'
            code = (vendor / filename).read_text(encoding="utf-8")
            page = page.replace(tag, "<script>\n" + code + "\n</script>")
        # Durable hosts: the native transport takes over send, so the right panel keeps what
        # works there (Tools, MCP) and opens on Tools; deploy/setup have no backend either.
        page = page.replace('</head>', NATIVE_PANEL_HIDE + '</head>', 1)
        # The page's own </body> is the last one: the inlined DOMPurify carries "<body>" strings.
        head, body_end, tail = page.rpartition('</body>')
        page = head + NATIVE_PANEL_OPEN_TOOLS + body_end + tail
    return (page.replace("{APX_TABLE_CSS}", TABLE_CSS).replace("{APX_TABLE_JS}", TABLE_JS)
            .replace("{APX_NATIVE_CHAT}", native_chat_script(root=False) if native else ""))


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
