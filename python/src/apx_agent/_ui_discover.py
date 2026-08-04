"""Dev UI — /_apx/discover workspace agent + UC tool + API browser with wire-back."""

from __future__ import annotations

from ._ui_nav import _apx_nav_css, _apx_nav_html, _deploy_overlay_html


def render_discover_ui() -> str:
    """Render Discover: browse peers/tools/APIs and wire them into agent.py."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Discover · APX dev</title>
  <style>
    {_apx_nav_css()}
    :root {{
      --bg:#0a0a0a; --panel:#111; --border:#2a2a2a; --text:#e5e7eb;
      --muted:#888; --accent:#60b0ff; --accent-bg:#0d1f38; --accent-border:#1e3a5f;
      --ok:#4ade80; --ok-bg:#052e1c;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text);
           font:13px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif; }}
    main {{ max-width:960px; margin:0 auto; padding:64px 20px 48px; }}
    h1 {{ font-size:18px; margin:0 0 4px; }}
    .sub {{ color:var(--muted); margin:0 0 20px; font-size:12px; }}
    .row {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:14px; }}
    button, .btn {{ font:inherit; cursor:pointer; color:var(--accent); background:var(--accent-bg);
                    border:1px solid var(--accent-border); border-radius:6px; padding:6px 12px; }}
    button:disabled {{ opacity:.5; cursor:default; }}
    button.secondary {{ color:#bbb; background:#161616; border-color:#333; }}
    input, select {{ font:inherit; background:#0d0d0d; color:var(--text);
                    border:1px solid var(--border); border-radius:6px; padding:6px 10px; }}
    .card {{ background:var(--panel); border:1px solid var(--border); border-radius:8px;
             padding:12px 14px; margin-bottom:8px; }}
    .card h3 {{ margin:0 0 4px; font-size:13px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
    .card-actions {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px; }}
    .pill {{ font-size:10px; text-transform:uppercase; letter-spacing:.4px;
             padding:2px 6px; border-radius:4px; background:#1a1a1a; color:var(--muted); }}
    .pill.app {{ color:#6ee7b7; background:#052e1c; }}
    .pill.uc {{ color:#fbbf24; background:#2a1f05; }}
    .pill.serving_endpoint {{ color:#93c5fd; background:#0c1a33; }}
    .pill.genie_space {{ color:#c4b5fd; background:#1e1533; }}
    .pill.vector_search_index {{ color:#5eead4; background:#042f2e; }}
    .desc {{ color:var(--muted); font-size:12px; margin:0 0 8px; }}
    .meta {{ font-size:11px; color:#666; font-family:ui-monospace,monospace; }}
    .tools {{ display:flex; flex-wrap:wrap; gap:4px; margin-top:8px; }}
    .tool {{ font-size:11px; background:#161616; border:1px solid #2a2a2a;
             border-radius:4px; padding:2px 7px; color:#bbb; }}
    .empty {{ color:var(--muted); padding:16px 0; }}
    .err {{ color:#f87171; font-size:12px; }}
    .hint {{ color:#666; font-size:11px; }}
    a.url {{ color:var(--accent); text-decoration:none; word-break:break-all; }}
    a.url:hover {{ text-decoration:underline; }}
    section {{ margin-bottom:28px; }}
    section h2 {{ font-size:14px; margin:0 0 10px; }}
    #wire-banner {{ display:none; margin:0 0 16px; padding:10px 14px; border-radius:8px;
                    background:var(--ok-bg); border:1px solid #14532d; color:var(--ok); }}
    #wire-banner.show {{ display:block; }}
    #wire-banner a {{ color:#86efac; }}
    #target-bar {{ margin-bottom:16px; }}
  </style>
</head>
<body>
{_apx_nav_html("discover")}
<main>
  <h1>Discover</h1>
  <p class="sub">Find workspace agents, UC functions, Genie spaces, and Vector Search indexes —
     then wire them into this agent. Changes apply live to Chat when possible (no deploy);
     agent.py is still updated so the next restart stays in sync.
     On a deployed App, wire/unwire needs <code>?token=</code> matching <code>APX_DEV_UI_TOKEN</code>
     (shared live-agent mutation).</p>

  <div id="wire-banner"></div>

  <div class="row" id="target-bar">
    <label class="meta" for="wire-target">Wire into</label>
    <select id="wire-target" title="Leaf Agent that receives sub_agents= / tools="></select>
    <span id="targets-status" class="meta"></span>
  </div>

  <section>
    <div class="row">
      <h2 style="margin:0;flex:1">Workspace agents</h2>
      <button id="btn-scan-agents">Refresh</button>
      <span id="agents-status" class="meta"></span>
    </div>
    <div id="agents-list" class="empty">Scanning workspace…</div>
  </section>

  <section>
    <h2>UC functions (tools)</h2>
    <div class="row">
      <input id="fn-catalog" placeholder="catalog" style="width:140px" />
      <input id="fn-schema" placeholder="schema" style="width:140px" />
      <button id="btn-scan-fns">List functions</button>
      <span id="fns-status" class="meta"></span>
    </div>
    <div id="fns-list" class="empty">Pick a catalog.schema to list Unity Catalog functions
      you can wire as tools.</div>
  </section>

  <section>
    <div class="row">
      <h2 style="margin:0;flex:1">APIs</h2>
      <button id="btn-scan-apis">Refresh</button>
      <span id="apis-status" class="meta"></span>
    </div>
    <div id="apis-list" class="empty">Scanning serving endpoints, Genie, and Vector Search…</div>
  </section>
</main>
{_deploy_overlay_html()}
<script>
const df = (url, init) => (window.apxDevFetch || fetch)(url, init);

function esc(s) {{
  return String(s||'').replace(/[&<>"']/g, c => ({{
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }})[c]);
}}

function showBanner(msg, live) {{
  const el = document.getElementById('wire-banner');
  const extra = live
    ? ' Ready in Chat now — no deploy needed.'
    : ' Could not hot-apply; restart / redeploy to load.';
  el.innerHTML = esc(msg) + extra + ' <a href="/_apx/agent?wired=1">Open Chat</a>';
  el.classList.add('show');
}}

async function loadTargets() {{
  const sel = document.getElementById('wire-target');
  const st = document.getElementById('targets-status');
  try {{
    const r = await fetch('/_apx/discover/targets');
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    window._discoverTargets = d.targets || [];
    const eligible = window._discoverTargets.filter(t => t.eligible);
    const all = window._discoverTargets;
    if (!eligible.length) {{
      sel.innerHTML = '<option value="">(no Agent leaf in agent.py)</option>';
      st.textContent = d.source_path
        ? 'No eligible Agent/LlmAgent — composition roots cannot take sub_agents='
        : 'No agent.py on disk';
      return;
    }}
    sel.innerHTML = eligible.map(t =>
      '<option value="'+esc(t.name)+'">'+esc(t.name)+' ('+esc(t.kind)+')'
      + (t.sub_agents && t.sub_agents.length ? ' · '+t.sub_agents.length+' peers' : '')
      + '</option>'
    ).join('');
    const ineligible = all.filter(t => !t.eligible).length;
    st.textContent = eligible.length + ' leaf' + (eligible.length===1?'':'s')
      + (ineligible ? ' · '+ineligible+' composition root(s) skipped' : '');
  }} catch (e) {{
    st.innerHTML = '<span class="err">'+esc(String(e.message||e))+'</span>';
  }}
}}

function selectedTarget() {{
  return document.getElementById('wire-target').value || 'agent';
}}

function peerRefFor(a) {{
  const label = (a.app_name || a.name || 'peer').replace(/[^A-Za-z0-9]+/g, '_').replace(/^_|_$/g, '').toUpperCase();
  return '$APX_PEER_' + (label || 'PEER') + '_URL';
}}

function isPeerWired(a) {{
  const ref = peerRefFor(a);
  const tname = selectedTarget();
  const t = (window._discoverTargets || []).find(x => x.name === tname);
  return !!(t && (t.sub_agents || []).includes(ref));
}}

async function wireAgent(a, btn) {{
  btn.disabled = true;
  try {{
    const r = await df('/_apx/discover/wire-agent', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        url: a.url,
        name: a.name,
        app_name: a.app_name || a.name,
        target: selectedTarget(),
        use_env: true
      }})
    }});
    const d = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(d.detail || d.error || await r.text());
    if (d.already_present && d.applied_live) {{
      showBanner('Already live as '+d.ref+' on '+d.target+'.', true);
    }} else if (d.already_present) {{
      showBanner('Already wired as '+d.ref+' on '+d.target+'.', !!d.applied_live);
    }} else {{
      showBanner('Wired '+d.ref+' into '+d.target+'.', !!d.applied_live);
      await loadTargets();
      scanAgents();
    }}
  }} catch (e) {{
    alert('Wire failed: '+(e.message||e));
  }} finally {{
    btn.disabled = false;
  }}
}}

async function unwireAgent(a, btn) {{
  btn.disabled = true;
  try {{
    const r = await df('/_apx/discover/unwire-agent', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{
        url: a.url,
        name: a.name,
        app_name: a.app_name || a.name,
        target: selectedTarget(),
        use_env: true
      }})
    }});
    const d = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    showBanner('Removed '+d.ref+' from '+d.target+'.', !!d.applied_live);
    await loadTargets();
    scanAgents();
  }} catch (e) {{
    alert('Unwire failed: '+(e.message||e));
  }} finally {{
    btn.disabled = false;
  }}
}}

async function wireTool(body, btn) {{
  btn.disabled = true;
  try {{
    const r = await df('/_apx/discover/wire-tool', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify(Object.assign({{ target: selectedTarget() }}, body))
    }});
    const d = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    showBanner('Attached '+d.binding_name+' to '+d.target+'.', !!d.applied_live);
  }} catch (e) {{
    alert('Attach failed: '+(e.message||e));
  }} finally {{
    btn.disabled = false;
  }}
}}

async function unwireTool(binding, btn) {{
  btn.disabled = true;
  try {{
    const r = await df('/_apx/discover/unwire-tool', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ kind: 'uc_function', binding_name: binding, target: selectedTarget() }})
    }});
    const d = await r.json().catch(() => ({{}}));
    if (!r.ok) throw new Error(d.detail || d.error || JSON.stringify(d));
    showBanner('Removed tool '+binding+'.', !!d.applied_live);
  }} catch (e) {{
    alert('Unwire failed: '+(e.message||e));
  }} finally {{
    btn.disabled = false;
  }}
}}

async function scanAgents() {{
  const btn = document.getElementById('btn-scan-agents');
  const st = document.getElementById('agents-status');
  const list = document.getElementById('agents-list');
  btn.disabled = true; st.textContent = 'Scanning…';
  try {{
    const r = await fetch('/_apx/workspace-agents');
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    const agents = d.agents || [];
    st.textContent = agents.length + ' found';
    if (!agents.length) {{
      list.innerHTML = '<div class="empty">No apx agents answered in this workspace. '
        + 'Deploy an Apps agent that serves /.well-known/agent.json, or register a UC model '
        + 'with the apx.agent.name tag.</div>';
      return;
    }}
    list.innerHTML = agents.map((a, i) => {{
      const tools = (a.tools||[]).map(t => '<span class="tool">'+esc(t)+'</span>').join('');
      const url = a.url
        ? '<div class="meta"><a class="url" href="'+esc(a.url)+'" target="_blank">'+esc(a.url)+'</a></div>'
        : (a.model_endpoint
            ? '<div class="meta">endpoint: '+esc(a.model_endpoint)+'</div>'
            : '');
      const uc = a.uc_name ? '<div class="meta">uc: '+esc(a.uc_name)+'</div>' : '';
      const action = a.url
        ? (isPeerWired(a)
          ? '<div class="card-actions"><button type="button" class="secondary" data-unwire-agent="'+i+'">Unwire</button></div>'
          : '<div class="card-actions"><button type="button" data-wire-agent="'+i+'">Add as sub-agent</button></div>')
        : '<div class="card-actions"><button type="button" disabled title="No Apps URL — UC-only models cannot be A2A sub-agents">Add as sub-agent</button>'
          + '<span class="hint">Needs an Apps URL to wire</span></div>';
      return '<div class="card" data-agent-idx="'+i+'">'
        + '<h3><span class="pill '+esc(a.source)+'">'+esc(a.source)+'</span> '
        + esc(a.name)
        + (a.state ? ' <span class="pill">'+esc(a.state)+'</span>' : '')
        + (a.url && isPeerWired(a) ? ' <span class="pill app">wired</span>' : '')
        + '</h3>'
        + (a.description ? '<p class="desc">'+esc(a.description)+'</p>' : '')
        + url + uc
        + '<div class="meta">'+(a.tool_count||0)+' tools'
        + (a.app_name ? ' · app '+esc(a.app_name) : '')+'</div>'
        + (tools ? '<div class="tools">'+tools+'</div>' : '')
        + action
        + '</div>';
    }}).join('');
    list._agents = agents;
    list.querySelectorAll('[data-wire-agent]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const a = list._agents[Number(btn.getAttribute('data-wire-agent'))];
        if (a) wireAgent(a, btn);
      }});
    }});
    list.querySelectorAll('[data-unwire-agent]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const a = list._agents[Number(btn.getAttribute('data-unwire-agent'))];
        if (a) unwireAgent(a, btn);
      }});
    }});
  }} catch (e) {{
    st.innerHTML = '<span class="err">'+esc(String(e.message||e))+'</span>';
    list.innerHTML = '<div class="empty err">Discovery failed. Use Refresh to retry.</div>';
  }} finally {{
    btn.disabled = false;
  }}
}}

async function scanFns() {{
  const catalog = document.getElementById('fn-catalog').value.trim();
  const schema = document.getElementById('fn-schema').value.trim();
  const btn = document.getElementById('btn-scan-fns');
  const st = document.getElementById('fns-status');
  const list = document.getElementById('fns-list');
  if (!catalog || !schema) {{ st.innerHTML = '<span class="err">catalog and schema required</span>'; return; }}
  btn.disabled = true; st.textContent = 'Listing…';
  try {{
    const q = new URLSearchParams({{catalog, schema}});
    const r = await fetch('/_apx/workspace-functions?'+q);
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    const fns = d.functions || [];
    st.textContent = fns.length + ' functions';
    if (!fns.length) {{
      list.innerHTML = '<div class="empty">No functions in '+esc(catalog)+'.'+esc(schema)+'.</div>';
      return;
    }}
    list.innerHTML = fns.map((f, i) =>
      '<div class="card"><h3>'+esc(f.full_name)+'</h3>'
      + (f.comment ? '<p class="desc">'+esc(f.comment)+'</p>' : '')
      + '<div class="card-actions"><button type="button" data-wire-fn="'+i+'">Attach as tool</button></div>'
      + '</div>'
    ).join('');
    list._fns = fns;
    list.querySelectorAll('[data-wire-fn]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const f = list._fns[Number(btn.getAttribute('data-wire-fn'))];
        if (f) wireTool({{ kind: 'uc_function', full_name: f.full_name }}, btn);
      }});
    }});
  }} catch (e) {{
    st.innerHTML = '<span class="err">'+esc(String(e.message||e))+'</span>';
  }} finally {{
    btn.disabled = false;
  }}
}}

async function scanApis() {{
  const btn = document.getElementById('btn-scan-apis');
  const st = document.getElementById('apis-status');
  const list = document.getElementById('apis-list');
  btn.disabled = true; st.textContent = 'Scanning…';
  try {{
    const r = await fetch('/_apx/workspace-apis');
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    const apis = d.apis || [];
    st.textContent = apis.length + ' found';
    if (!apis.length) {{
      list.innerHTML = '<div class="empty">No serving endpoints, Genie spaces, or Vector Search '
        + 'indexes visible to this identity.</div>';
      return;
    }}
    const labels = {{
      serving_endpoint: 'serving',
      genie_space: 'genie',
      vector_search_index: 'vector-search'
    }};
    list.innerHTML = apis.map((a, i) => {{
      const kindLabel = labels[a.kind] || a.kind;
      const invoke = a.url
        ? '<div class="meta">invoke: <a class="url" href="'+esc(a.url)+'" target="_blank">'+esc(a.url)+'</a></div>'
        : '';
      const mcp = a.mcp_url
        ? '<div class="meta">mcp: <a class="url" href="'+esc(a.mcp_url)+'" target="_blank">'+esc(a.mcp_url)+'</a></div>'
        : '';
      const spaceId = a.extra && a.extra.space_id
        ? '<div class="meta">space_id: '+esc(a.extra.space_id)+'</div>'
        : '';
      let action = '';
      if (a.kind === 'genie_space') {{
        action = '<div class="card-actions"><button type="button" data-wire-api="'+i+'">Attach as tool</button></div>';
      }} else if (a.kind === 'vector_search_index') {{
        action = '<div class="card-actions"><button type="button" data-wire-api="'+i+'">Attach as tool</button></div>';
      }} else if (a.kind === 'serving_endpoint') {{
        action = '<div class="card-actions"><span class="hint">Model endpoint — set as model= / use Playground, not a tool</span></div>';
      }}
      return '<div class="card">'
        + '<h3><span class="pill '+esc(a.kind)+'">'+esc(kindLabel)+'</span> '
        + esc(a.name)
        + (a.state ? ' <span class="pill">'+esc(a.state)+'</span>' : '')
        + '</h3>'
        + (a.description ? '<p class="desc">'+esc(a.description)+'</p>' : '')
        + invoke + mcp + spaceId
        + action
        + '</div>';
    }}).join('');
    list._apis = apis;
    list.querySelectorAll('[data-wire-api]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const a = list._apis[Number(btn.getAttribute('data-wire-api'))];
        if (!a) return;
        if (a.kind === 'genie_space') {{
          wireTool({{
            kind: 'genie_space',
            space_id: (a.extra && a.extra.space_id) || '',
            title: a.name
          }}, btn);
        }} else if (a.kind === 'vector_search_index') {{
          wireTool({{
            kind: 'vector_search_index',
            index_name: a.name,
            columns: (a.extra && a.extra.columns) || ['content']
          }}, btn);
        }}
      }});
    }});
  }} catch (e) {{
    st.innerHTML = '<span class="err">'+esc(String(e.message||e))+'</span>';
    list.innerHTML = '<div class="empty err">API discovery failed. Use Refresh to retry.</div>';
  }} finally {{
    btn.disabled = false;
  }}
}}

document.getElementById('btn-scan-agents').addEventListener('click', scanAgents);
document.getElementById('btn-scan-fns').addEventListener('click', scanFns);
document.getElementById('btn-scan-apis').addEventListener('click', scanApis);

loadTargets();
fetch('/_apx/workspace-context').then(r => r.json()).then(d => {{
  const c = (d.used_catalogs||[])[0];
  const s = (d.used_schemas||[])[0];
  if (c) document.getElementById('fn-catalog').value = c;
  if (s && s.includes('.')) document.getElementById('fn-schema').value = s.split('.')[1];
  if (c && s && s.includes('.')) scanFns();
}}).catch(() => {{}}).finally(() => {{
  scanAgents();
  scanApis();
}});
</script>
</body>
</html>"""
