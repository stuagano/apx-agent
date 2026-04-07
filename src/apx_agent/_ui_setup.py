"""Dev UI — Setup wizard, eval tab, wizard, and environment helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._models import AgentContext
from ._ui_edit import _find_deploy_root
from ._ui_nav import _apx_nav_css, _apx_nav_html, _deploy_overlay_html


def _find_env_path() -> "Path | None":
    """Return the .env file in the project root (creates it if absent)."""
    root = _find_deploy_root()
    if root is None:
        return None
    return root / ".env"


def _read_env_file(path: "Path") -> "dict[str, str]":
    """Parse a .env file into a dict, ignoring comments and blank lines."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _write_env_file(path: "Path", updates: "dict[str, str]") -> None:
    """Merge updates into an existing .env file, preserving comments and other vars."""
    lines: list[str] = []
    written: set[str] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    written.add(key)
                    continue
            lines.append(line)
    for k, v in updates.items():
        if k not in written:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n")


def _render_setup_ui(current: "dict[str, str]") -> str:
    """Setup wizard page — catalog/schema/warehouse picker + instruction generator."""
    import json as _json
    nav = _apx_nav_html("setup")
    overlay = _deploy_overlay_html()
    cur_catalog = current.get("DEMO_CATALOG", "")
    cur_schema = current.get("DEMO_SCHEMA", "")
    cur_wh = current.get("WAREHOUSE_ID", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Setup — APX Dev</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: #0d0d0d; color: #ccc; font-family: system-ui, sans-serif; font-size: 13px; }}
  {_apx_nav_css()}
  .page {{ max-width: 680px; margin: 72px auto 40px; padding: 0 20px; }}
  h2 {{ font-size: 18px; font-weight: 600; color: #fff; margin-bottom: 4px; }}
  .subtitle {{ color: #555; margin-bottom: 28px; font-size: 13px; }}
  .section {{ background: #111; border: 1px solid #1e1e1e; border-radius: 10px;
              padding: 20px; margin-bottom: 16px; }}
  .section-title {{ font-size: 11px; font-weight: 700; color: #555; text-transform: uppercase;
                    letter-spacing: .5px; margin-bottom: 14px; }}
  .field {{ margin-bottom: 14px; }}
  .field:last-child {{ margin-bottom: 0; }}
  label {{ display: block; font-size: 11px; font-weight: 600; color: #888;
           text-transform: uppercase; letter-spacing: .4px; margin-bottom: 5px; }}
  select, input[type=text] {{ width: 100%; background: #0d0d0d; border: 1px solid #2a2a2a;
    color: #ccc; border-radius: 6px; padding: 8px 10px; font-size: 13px; }}
  select:focus, input:focus {{ outline: none; border-color: #3a7bd5; }}
  select:disabled {{ opacity: .5; }}
  .btn-row {{ display: flex; gap: 8px; margin-top: 20px; align-items: center; }}
  .btn-primary {{ background: #2563eb; color: #fff; border: none; border-radius: 6px;
                  padding: 9px 20px; font-size: 13px; font-weight: 500; cursor: pointer; }}
  .btn-primary:hover {{ background: #1d4ed8; }}
  .btn-primary:disabled {{ opacity: .5; cursor: default; }}
  .btn-secondary {{ background: transparent; color: #888; border: 1px solid #333;
                    border-radius: 6px; padding: 9px 16px; font-size: 13px; cursor: pointer; }}
  .btn-secondary:hover {{ color: #ccc; border-color: #555; }}
  #status {{ flex: 1; font-size: 12px; }}
  #status.ok {{ color: #4ade80; }}
  #status.err {{ color: #f87171; }}
  #instructions-section {{ display: none; }}
  #instructions-box {{ width: 100%; min-height: 160px; background: #0d0d0d;
    border: 1px solid #2a2a2a; color: #ccc; border-radius: 6px; padding: 10px;
    font-size: 12px; font-family: system-ui, sans-serif; line-height: 1.6; resize: vertical; }}
  .note {{ font-size: 11px; color: #444; margin-top: 6px; }}
  .current-tag {{ display: inline-block; background: #0d2a0d; color: #4ade80;
                  border: 1px solid #1a4a1a; border-radius: 4px; padding: 1px 6px;
                  font-size: 10px; margin-left: 6px; vertical-align: middle; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .pcard {{ background: #0a0a0a; border: 1px solid #222; border-radius: 8px; padding: 12px;
            cursor: pointer; transition: border-color .15s, background .15s; }}
  .pcard:hover {{ border-color: #3a5a8a; background: #0d1520; }}
  .pcard.active {{ border-color: #2563eb; background: #0d1a30; }}
  .pcard-name {{ font-size: 12px; font-weight: 700; color: #ccc; margin-bottom: 3px; }}
  .pcard.active .pcard-name {{ color: #60b0ff; }}
  .pcard-tag {{ font-size: 10px; font-weight: 600; color: #555; text-transform: uppercase;
                letter-spacing: .4px; margin-bottom: 6px; }}
  .pcard.active .pcard-tag {{ color: #3a7bd5; }}
  .pcard-desc {{ font-size: 11px; color: #444; line-height: 1.5; }}
  .pcard.active .pcard-desc {{ color: #666; }}
  .pcard code {{ background: #111; padding: 1px 3px; border-radius: 3px; font-size: 10px; }}
  /* tool palette */
  .tcard {{ background: #0a0a0a; border: 1px solid #1e1e1e; border-radius: 8px; padding: 12px 14px; }}
  .tcard-header {{ display: flex; align-items: center; gap: 6px; margin-bottom: 4px; }}
  .tcard-name {{ font-family: monospace; font-size: 12px; font-weight: 700; color: #ccc; flex: 1;
                 min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .tcard-btn {{ background: none; border: none; color: #444; cursor: pointer; font-size: 11px;
                padding: 2px 7px; border-radius: 4px; line-height: 1.4; }}
  .tcard-btn:hover {{ color: #ccc; background: #1a1a1a; }}
  .tcard-del:hover {{ color: #f87171 !important; background: #1a0a0a !important; }}
  .tcard-desc {{ font-size: 11px; color: #444; line-height: 1.5; margin-bottom: 6px;
                 overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2;
                 -webkit-box-orient: vertical; }}
  .tcard-params {{ display: flex; flex-wrap: wrap; gap: 3px; }}
  .tcard-param {{ background: #111; border: 1px solid #1e1e1e; border-radius: 3px;
                  padding: 1px 6px; font-size: 10px; color: #555; font-family: monospace; }}
  @keyframes tcard-in {{ from {{ opacity:0; transform:scale(.97); }} to {{ opacity:1; transform:scale(1); }} }}
  .tcard-appear {{ animation: tcard-in .2s ease; }}
  /* agent composer nodes */
  .anode {{ background: #0d0d0d; border: 1px solid #222; border-radius: 10px; padding: 16px 18px; margin-bottom: 10px; }}
  .anode-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}
  .anode-name {{ font-family: monospace; font-size: 13px; font-weight: 700; color: #ddd; flex: 1; }}
  .anode-wrapper {{ font-size: 10px; font-weight: 600; color: #3a7bd5; background: #0d1a30;
                    border: 1px solid #1a3a6a; border-radius: 3px; padding: 1px 7px;
                    text-transform: uppercase; letter-spacing: .3px; }}
  .anode-label {{ font-size: 10px; font-weight: 700; color: #555; text-transform: uppercase;
                  letter-spacing: .4px; margin-bottom: 6px; }}
  .anode-tools {{ display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; min-height: 24px; }}
  .anode-tool {{ display: flex; align-items: center; gap: 4px; background: #111;
                 border: 1px solid #222; border-radius: 4px; padding: 2px 8px;
                 font-size: 11px; font-family: monospace; color: #888; cursor: pointer; }}
  .anode-tool:hover {{ border-color: #555; color: #ccc; }}
  .anode-tool.assigned {{ background: #0d1a10; border-color: #1a4a20; color: #4ade80; }}
  .anode-tool.assigned:hover {{ border-color: #22c55e; }}
  .anode-instructions {{ width: 100%; min-height: 80px; background: #0a0a0a;
    border: 1px solid #1e1e1e; color: #ccc; border-radius: 6px; padding: 8px 10px;
    font-size: 11px; font-family: system-ui, sans-serif; line-height: 1.6; resize: vertical;
    margin-bottom: 8px; }}
  .anode-instructions:focus {{ outline: none; border-color: #3a7bd5; }}
  .anode-behavior {{ width: 100%; min-height: 60px; background: #0a0a0a;
    border: 1px solid #2a2a2a; color: #ccc; border-radius: 6px; padding: 8px 10px;
    font-size: 12px; font-family: system-ui, sans-serif; line-height: 1.6; resize: vertical;
    margin-bottom: 8px; }}
  .anode-behavior:focus {{ outline: none; border-color: #3a7bd5; }}
  .anode-wire-btn {{ background: #1a3a6a; border: 1px solid #2a5aba; color: #7ab3ff;
    border-radius: 5px; padding: 5px 14px; font-size: 11px; font-weight: 600; cursor: pointer; }}
  .anode-wire-btn:hover {{ background: #224a8a; border-color: #4a7aea; color: #aad0ff; }}
  .anode-wire-btn:disabled {{ opacity: .5; cursor: not-allowed; }}
</style>
</head>
<body>
{nav}
<div class="page">
  <h2>Setup</h2>
  <p class="subtitle">Connect your agent to Unity Catalog data and generate a system prompt.</p>

  <div class="section">
    <div class="section-title">Data Source</div>
    <div class="field">
      <label>Catalog {('<span class="current-tag">' + cur_catalog + '</span>') if cur_catalog else ''}</label>
      <select id="sel-catalog"><option value="">Loading…</option></select>
    </div>
    <div class="field">
      <label>Schema {('<span class="current-tag">' + cur_schema + '</span>') if cur_schema else ''}</label>
      <select id="sel-schema" disabled><option value="">Select a catalog first</option></select>
    </div>
    <div class="field">
      <label>SQL Warehouse {('<span class="current-tag">' + cur_wh + '</span>') if cur_wh else ''}</label>
      <select id="sel-warehouse"><option value="">Loading…</option></select>
    </div>
  </div>

  <div class="section" id="tools-section">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="section-title" style="margin-bottom:0">Tools</div>
      <span id="tools-count" style="font-size:11px;color:#444"></span>
    </div>
    <!-- palette grid -->
    <div id="tool-palette" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px">
      <div style="color:#333;font-size:12px;grid-column:1/-1">Loading…</div>
    </div>
    <!-- divider -->
    <div style="border-top:1px solid #1a1a1a;margin-bottom:14px"></div>
    <!-- create from description -->
    <div class="field">
      <label>Describe a tool</label>
      <div style="display:flex;gap:8px">
        <input type="text" id="tool-desc-input"
               placeholder="e.g. Get the top 5 customers by total spend" style="flex:1">
        <button class="btn-primary" id="btn-desc-tool" style="white-space:nowrap">Generate</button>
      </div>
      <div id="desc-tool-status" style="font-size:12px;margin-top:6px;min-height:16px"></div>
    </div>
    <!-- create from table (collapsible) -->
    <details id="from-table-details">
      <summary style="cursor:pointer;color:#555;font-size:12px;user-select:none;
                      padding:4px 0;list-style:none;display:flex;align-items:center;gap:6px">
        <span style="font-size:10px">▶</span>
        <span>Generate from table</span>
      </summary>
      <div style="margin-top:10px">
        <div id="tools-table-list">
          <p style="color:#444;font-size:12px">Select a catalog and schema above to see tables.</p>
        </div>
        <div id="tools-gen-progress" style="margin-top:10px;font-size:12px;color:#60b0ff;min-height:18px"></div>
        <div class="btn-row" style="margin-top:14px">
          <button class="btn-primary" id="btn-gen-tools" disabled>Generate Selected</button>
          <span id="tools-status" style="font-size:12px"></span>
        </div>
      </div>
    </details>
  </div>

  <div class="section" id="agents-section">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="section-title" style="margin-bottom:0">Agents</div>
      <button class="btn-secondary" id="btn-add-node"
              style="padding:5px 12px;font-size:11px">+ Add agent</button>
    </div>
    <div id="agent-nodes"></div>
    <div class="btn-row" style="margin-top:14px">
      <button class="btn-primary" id="btn-apply-agents" disabled>Apply</button>
      <span id="agents-status" style="font-size:12px"></span>
    </div>
  </div>

  <div class="section" id="pattern-section">
    <div class="section-title">Agent Pattern</div>
    <p style="color:#555;font-size:12px;margin-bottom:14px;line-height:1.6">
      Choose how your agent executes. Click a pattern to apply it or see the code.
    </p>
    <div id="pattern-cards" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px">
      <div class="pcard" data-pattern="Agent" data-auto="1">
        <div class="pcard-name">LlmAgent</div>
        <div class="pcard-tag">Single loop</div>
        <div class="pcard-desc">Default. One agent, all tools, loops until a final response.</div>
      </div>
      <div class="pcard" data-pattern="LoopAgent" data-auto="1">
        <div class="pcard-name">LoopAgent</div>
        <div class="pcard-tag">Iterative</div>
        <div class="pcard-desc">Keeps iterating across turns until it calls <code>finish_loop()</code>.</div>
      </div>
      <div class="pcard" data-pattern="SequentialAgent" data-auto="0">
        <div class="pcard-name">SequentialAgent</div>
        <div class="pcard-tag">Pipeline</div>
        <div class="pcard-desc">Chain agents — each stage receives the previous output as context.</div>
      </div>
      <div class="pcard" data-pattern="ParallelAgent" data-auto="0">
        <div class="pcard-name">ParallelAgent</div>
        <div class="pcard-tag">Concurrent</div>
        <div class="pcard-desc">All branches run concurrently with the same input, results merged.</div>
      </div>
      <div class="pcard" data-pattern="RouterAgent" data-auto="0">
        <div class="pcard-name">RouterAgent</div>
        <div class="pcard-tag">Route to expert</div>
        <div class="pcard-desc">One routing call picks a specialist agent per request.</div>
      </div>
      <div class="pcard" data-pattern="HandoffAgent" data-auto="0">
        <div class="pcard-name">HandoffAgent</div>
        <div class="pcard-tag">Pass the baton</div>
        <div class="pcard-desc">Active agent transfers control to another mid-turn.</div>
      </div>
    </div>
    <div id="pattern-snippet-wrap" style="display:none;margin-top:8px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
        <span style="font-size:11px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.4px">
          Paste into agent_router.py
        </span>
        <div style="display:flex;gap:8px">
          <button class="btn-secondary" id="btn-copy-snippet" style="padding:5px 12px;font-size:11px">Copy</button>
          <a href="/_apx/edit" style="color:#60b0ff;font-size:11px;text-decoration:none;
             border:1px solid #2a4a6a;border-radius:6px;padding:5px 12px">Open Editor →</a>
        </div>
      </div>
      <pre id="pattern-snippet" style="background:#0a0a0a;border:1px solid #1e1e1e;border-radius:6px;
           padding:12px;font-size:11px;color:#ccc;overflow-x:auto;white-space:pre;margin:0;line-height:1.5"></pre>
    </div>
    <div id="pattern-status" style="margin-top:8px;font-size:12px;min-height:16px"></div>
  </div>

  <div class="section" id="instructions-section">
    <div class="section-title">Agent Instructions</div>
    <p style="color:#555;font-size:12px;margin-bottom:12px">
      Generated from your schema — edit before applying.
    </p>
    <div class="field">
      <textarea id="instructions-box" placeholder="Instructions will appear here…"></textarea>
      <p class="note">These will be written to <code>pyproject.toml</code> as <code>instructions</code>
        under <code>[tool.apx.agent]</code> and applied immediately.</p>
    </div>
    <div class="btn-row">
      <button class="btn-secondary" id="btn-regen">↺ Regenerate</button>
      <button class="btn-primary" id="btn-apply">Apply Instructions</button>
      <span id="apply-status" style="font-size:12px"></span>
    </div>
  </div>

  <div class="btn-row">
    <button class="btn-primary" id="btn-save">Save &amp; Generate Instructions</button>
    <span id="status"></span>
  </div>

  <div class="section" style="margin-top:28px">
    <div class="section-title">Diagnostics</div>

    <details style="margin-bottom:16px">
      <summary style="cursor:pointer;color:#888;font-size:12px;user-select:none;padding:4px 0">▶ Connectivity Test</summary>
      <div style="margin-top:10px">
        <p style="color:#555;font-size:12px;margin-bottom:10px;line-height:1.6">
          Test outbound connectivity from this deployment. The request runs server-side,
          so results reflect the network path of your deployed app, not your browser.
        </p>
        <div style="display:flex;gap:8px;margin-bottom:10px">
          <input id="probe-url" type="text" placeholder="https://api.example.com/health"
                 style="flex:1" value="">
          <button class="btn-primary" id="btn-probe" style="white-space:nowrap">Test</button>
        </div>
        <div id="probe-result"></div>
      </div>
    </details>

    <details id="vs-details">
      <summary style="cursor:pointer;color:#888;font-size:12px;user-select:none;padding:4px 0">▶ Vector Search Indexes</summary>
      <div id="vs-content" style="margin-top:10px">
        <p style="color:#555;font-size:12px">Click to discover indexes in this workspace.</p>
      </div>
    </details>
  </div>
</div>

<script>
const CUR_CATALOG = {_json.dumps(cur_catalog)};
const CUR_SCHEMA  = {_json.dumps(cur_schema)};
const CUR_WH      = {_json.dumps(cur_wh)};

async function loadCatalogs() {{
  const r = await fetch('/_apx/setup/catalogs');
  const d = await r.json();
  const sel = document.getElementById('sel-catalog');
  sel.innerHTML = '<option value="">— select —</option>' +
    d.map(c => `<option value="${{c}}"${{c===CUR_CATALOG?' selected':''}}>${{c}}</option>`).join('');
  if (CUR_CATALOG) loadSchemas(CUR_CATALOG);
}}

async function loadSchemas(catalog) {{
  const sel = document.getElementById('sel-schema');
  sel.disabled = true;
  sel.innerHTML = '<option>Loading…</option>';
  const r = await fetch('/_apx/setup/schemas?catalog=' + encodeURIComponent(catalog));
  const d = await r.json();
  sel.innerHTML = '<option value="">— select —</option>' +
    d.map(s => `<option value="${{s}}"${{s===CUR_SCHEMA?' selected':''}}>${{s}}</option>`).join('');
  sel.disabled = false;
}}

async function loadWarehouses() {{
  const r = await fetch('/_apx/setup/warehouses');
  const d = await r.json();
  const sel = document.getElementById('sel-warehouse');
  sel.innerHTML = '<option value="">— select —</option>' +
    d.map(w => `<option value="${{w.id}}"${{w.id===CUR_WH?' selected':''}}>${{w.name}} (${{w.state}})</option>`).join('');
}}

document.getElementById('sel-catalog').addEventListener('change', e => {{
  if (e.target.value) loadSchemas(e.target.value);
}});

document.getElementById('btn-save').addEventListener('click', async () => {{
  const catalog = document.getElementById('sel-catalog').value;
  const schema  = document.getElementById('sel-schema').value;
  const wh      = document.getElementById('sel-warehouse').value;
  const status  = document.getElementById('status');
  if (!catalog || !schema || !wh) {{
    status.textContent = 'Select catalog, schema, and warehouse.';
    status.className = 'err'; return;
  }}
  const btn = document.getElementById('btn-save');
  btn.disabled = true; btn.textContent = 'Saving…';
  status.textContent = ''; status.className = '';
  try {{
    const r = await fetch('/_apx/setup', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{catalog, schema, warehouse_id: wh, generate_instructions: true}}),
    }});
    const d = await r.json();
    if (!d.ok) {{ status.textContent = d.error; status.className = 'err'; return; }}
    status.textContent = '✓ Saved'; status.className = 'ok';
    if (d.instructions) {{
      document.getElementById('instructions-box').value = d.instructions;
      document.getElementById('instructions-section').style.display = 'block';
    }}
  }} catch(e) {{
    status.textContent = e.message; status.className = 'err';
  }} finally {{
    btn.disabled = false; btn.textContent = 'Save & Generate Instructions';
  }}
}});

document.getElementById('btn-regen').addEventListener('click', async () => {{
  const btn = document.getElementById('btn-regen');
  btn.disabled = true; btn.textContent = 'Generating…';
  const catalog = document.getElementById('sel-catalog').value || CUR_CATALOG;
  const schema  = document.getElementById('sel-schema').value  || CUR_SCHEMA;
  const wh      = document.getElementById('sel-warehouse').value || CUR_WH;
  try {{
    const r = await fetch('/_apx/setup/generate-instructions', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{catalog, schema, warehouse_id: wh}}),
    }});
    const d = await r.json();
    if (d.ok) document.getElementById('instructions-box').value = d.instructions;
    else alert(d.error);
  }} finally {{ btn.disabled = false; btn.textContent = '↺ Regenerate'; }}
}});

document.getElementById('btn-apply').addEventListener('click', async () => {{
  const instructions = document.getElementById('instructions-box').value.trim();
  if (!instructions) return;
  const st = document.getElementById('apply-status');
  const btn = document.getElementById('btn-apply');
  btn.disabled = true;
  try {{
    const r = await fetch('/_apx/setup/apply-instructions', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{instructions}}),
    }});
    const d = await r.json();
    st.textContent = d.ok ? '✓ Applied' : d.error;
    st.style.color = d.ok ? '#4ade80' : '#f87171';
  }} finally {{ btn.disabled = false; }}
}});

// ── Tool Palette ──
async function loadTools() {{
  const palette  = document.getElementById('tool-palette');
  const countEl  = document.getElementById('tools-count');
  try {{
    const r     = await fetch('/_apx/setup/tools');
    const tools = await r.json();
    countEl.textContent = tools.length ? `${{tools.length}} tool${{tools.length===1?'':'s'}}` : '';
    if (!tools.length) {{
      palette.innerHTML = '<div style="color:#333;font-size:12px;grid-column:1/-1;padding:4px 0">No tools yet — generate from a table or describe one below.</div>';
      return;
    }}
    palette.innerHTML = tools.map(t => {{
      const params = (t.params||[]).map(p =>
        `<span class="tcard-param">${{p.name}}: ${{p.type}}</span>`).join('');
      return `<div class="tcard" data-name="${{t.name}}">
        <div class="tcard-header">
          <span class="tcard-name" title="${{t.name}}">${{t.name}}</span>
          <a href="/_apx/edit" class="tcard-btn" title="Open in editor"
             style="text-decoration:none;font-size:10px">Edit</a>
          <button class="tcard-btn tcard-del" onclick="deleteTool('${{t.name}}')"
                  title="Delete tool">✕</button>
        </div>
        ${{t.description ? `<div class="tcard-desc" title="${{t.description}}">${{t.description}}</div>` : ''}}
        <div class="tcard-params">${{params}}</div>
      </div>`;
    }}).join('');
  }} catch(e) {{
    palette.innerHTML = `<div style="color:#f87171;font-size:12px;grid-column:1/-1">${{e.message}}</div>`;
  }}
}}

async function deleteTool(name) {{
  if (!confirm(`Delete "${{name}}"? This removes it from agent_router.py.`)) return;
  const r = await fetch(`/_apx/tools/${{encodeURIComponent(name)}}`, {{ method: 'DELETE' }});
  const d = await r.json();
  if (d.ok) {{
    const card = document.querySelector(`.tcard[data-name="${{name}}"]`);
    if (card) card.remove();
    const remaining = document.querySelectorAll('.tcard').length;
    document.getElementById('tools-count').textContent =
      remaining ? `${{remaining}} tool${{remaining===1?'':'s'}}` : '';
    if (!remaining) document.getElementById('tool-palette').innerHTML =
      '<div style="color:#333;font-size:12px;grid-column:1/-1;padding:4px 0">No tools yet.</div>';
  }} else {{ alert(d.error || 'Delete failed'); }}
}}

// create from description
document.getElementById('btn-desc-tool').addEventListener('click', async () => {{
  const desc = document.getElementById('tool-desc-input').value.trim();
  if (!desc) return;
  const btn = document.getElementById('btn-desc-tool');
  const st  = document.getElementById('desc-tool-status');
  btn.disabled = true;
  st.style.color = '#60b0ff'; st.textContent = 'Generating…';
  try {{
    const r = await fetch('/_apx/setup/create-tool', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ description: desc }}),
    }});
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    st.style.color = '#4ade80';
    st.textContent = `✓ Created ${{d.tool_name || ''}}`;
    document.getElementById('tool-desc-input').value = '';
    await loadTools();
    const cards = document.querySelectorAll('.tcard');
    if (cards.length) cards[cards.length-1].classList.add('tcard-appear');
  }} catch(e) {{
    st.style.color = '#f87171'; st.textContent = e.message;
  }} finally {{ btn.disabled = false; }}
}});

// generate from table (refresh palette after)
let tableList = [];

async function loadTables(catalog, schema) {{
  const listEl = document.getElementById('tools-table-list');
  const btn    = document.getElementById('btn-gen-tools');
  listEl.innerHTML = '<span style="color:#555;font-size:12px">Loading tables… <span style="display:inline-block;width:10px;height:10px;border:2px solid #333;border-top-color:#60b0ff;border-radius:50%;animation:spin .7s linear infinite"></span></span>';
  btn.disabled = true;
  try {{
    const r = await fetch(`/_apx/wizard/tables?catalog=${{encodeURIComponent(catalog)}}&schema=${{encodeURIComponent(schema)}}`);
    tableList = await r.json();
    if (!tableList.length) {{
      listEl.innerHTML = '<p style="color:#444;font-size:12px">No tables found in this schema.</p>';
      return;
    }}
    listEl.innerHTML = tableList.map((t, i) => {{
      const cols = t.columns.map(c => `<span style="background:#1a1a1a;border:1px solid #222;border-radius:3px;padding:1px 6px;font-size:10px;color:#666;margin-right:3px">${{c.name}}</span>`).join('');
      const rc = t.row_count != null ? `<span style="font-size:10px;color:#444;margin-left:auto">${{t.row_count.toLocaleString()}} rows</span>` : '';
      return `<label style="display:flex;align-items:flex-start;gap:10px;padding:10px 12px;background:#111;border:1px solid #1e1e1e;border-radius:6px;margin-bottom:6px;cursor:pointer">
        <input type="checkbox" data-idx="${{i}}" checked style="margin-top:2px;flex-shrink:0">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:5px">
            <span style="font-weight:600;color:#ccc;font-size:13px">${{t.name}}</span>
            ${{rc}}
          </div>
          <div style="flex-wrap:wrap;display:flex;gap:3px">${{cols}}</div>
        </div>
      </label>`;
    }}).join('');
    btn.disabled = false;
  }} catch(e) {{
    listEl.innerHTML = `<p style="color:#f87171;font-size:12px">${{e.message}}</p>`;
  }}
}}

document.getElementById('btn-gen-tools').addEventListener('click', async () => {{
  const catalog = document.getElementById('sel-catalog').value || CUR_CATALOG;
  const schema  = document.getElementById('sel-schema').value  || CUR_SCHEMA;
  const wh      = document.getElementById('sel-warehouse').value || CUR_WH;
  const checked = [...document.querySelectorAll('#tools-table-list input[type=checkbox]:checked')];
  if (!checked.length) {{ document.getElementById('tools-status').textContent = 'Select at least one table.'; return; }}
  const btn  = document.getElementById('btn-gen-tools');
  const prog = document.getElementById('tools-gen-progress');
  const st   = document.getElementById('tools-status');
  btn.disabled = true; st.textContent = ''; prog.innerHTML = '';
  let created = 0;
  for (const cb of checked) {{
    const t = tableList[+cb.dataset.idx];
    prog.innerHTML += `<div id="tp-${{t.name}}"><span style="display:inline-block;width:8px;height:8px;border:1px solid #333;border-top-color:#60b0ff;border-radius:50%;animation:spin .7s linear infinite"></span> Generating <strong>${{t.name}}</strong>…</div>`;
    try {{
      const r = await fetch('/_apx/wizard/generate-tools', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{ table: t.name,
          description: `Query the ${{t.name}} table. Columns: ${{t.columns.map(c=>c.name).join(', ')}}.`,
          catalog, schema, warehouse_id: wh }}),
      }});
      const d = await r.json();
      document.getElementById(`tp-${{t.name}}`).innerHTML =
        `<span style="color:#22c55e">✓</span> <strong>${{t.name}}</strong>: ${{d.tool_name || 'created'}}`;
      created++;
    }} catch(e) {{
      document.getElementById(`tp-${{t.name}}`).innerHTML =
        `<span style="color:#f87171">✗</span> <strong>${{t.name}}</strong>: ${{e.message}}`;
    }}
  }}
  btn.disabled = false;
  st.textContent = `${{created}}/${{checked.length}} tools created`;
  st.style.color = created === checked.length ? '#22c55e' : '#f59e0b';
  if (created > 0) await loadTools();
}});

// trigger table load when schema changes
document.getElementById('sel-schema').addEventListener('change', e => {{
  const catalog = document.getElementById('sel-catalog').value;
  if (e.target.value && catalog) loadTables(catalog, e.target.value);
}});

// Load palette on page load
loadTools();

// ── Agent Composer ──
let agentNodeState = []; // [{name, tools, instructions, behavior, wrapper}]

async function loadAgentNodes() {{
  try {{
    const r = await fetch('/_apx/setup/agents');
    const nodes = await r.json();
    // Preserve behavior field if we already have state for this node
    agentNodeState = nodes.map(n => {{
      const existing = agentNodeState.find(e => e.name === n.name);
      return {{ behavior: existing?.behavior || '', ...n }};
    }});
    renderAgentNodes();
  }} catch(e) {{
    document.getElementById('agent-nodes').innerHTML =
      `<div style="color:#f87171;font-size:12px">${{e.message}}</div>`;
  }}
}}

function renderAgentNodes() {{
  const container = document.getElementById('agent-nodes');
  const applyBtn  = document.getElementById('btn-apply-agents');
  applyBtn.disabled = !agentNodeState.length;

  if (!agentNodeState.length) {{
    container.innerHTML = '<div style="color:#333;font-size:12px;padding:4px 0">No agents yet — click "Add agent" to define one.</div>';
    return;
  }}

  // Get all tool names from palette
  const allTools = [...document.querySelectorAll('.tcard')].map(c => c.dataset.name).filter(Boolean);

  container.innerHTML = agentNodeState.map((node, idx) => {{
    const toolPills = node.tools.length
      ? node.tools.map(t => `<span class="anode-tool assigned"
          onclick="toggleNodeTool(${{idx}},'${{t}}')" title="Click to remove">✓ ${{t}}</span>`).join('')
      + (allTools.filter(t => !node.tools.includes(t)).map(t =>
          `<span class="anode-tool" onclick="toggleNodeTool(${{idx}},'${{t}}')" title="Click to add">${{t}}</span>`
        ).join(''))
      : allTools.map(t => `<span class="anode-tool" onclick="toggleNodeTool(${{idx}},'${{t}}')">${{t}}</span>`).join('');
    const wrapBadge = node.wrapper
      ? `<span class="anode-wrapper">${{node.wrapper}}</span>` : '';
    return `<div class="anode" data-idx="${{idx}}">
      <div class="anode-header">
        <span class="anode-name">${{node.name}}</span>
        ${{wrapBadge}}
      </div>
      <div class="anode-label">What should this agent do?</div>
      <textarea class="anode-behavior" id="anode-behavior-${{idx}}"
                oninput="agentNodeState[${{idx}}].behavior=this.value"
                placeholder="e.g. Look up a customer's billing history, explain their charges in plain language, and answer questions about their rate plan.">${{node.behavior||''}}</textarea>
      <button class="anode-wire-btn" id="anode-wire-${{idx}}"
              onclick="wireAgent(${{idx}})">Wire tools &amp; draft ✦</button>
      <div class="anode-label" style="margin-top:12px">Tools
        <span style="font-size:10px;color:#444;font-weight:400;margin-left:6px">assigned by wiring · click to toggle</span>
      </div>
      <div class="anode-tools" id="anode-tools-${{idx}}">${{toolPills || '<span style="color:#333;font-size:11px">Generate tools above, then wire this agent.</span>'}}</div>
      <div class="anode-label" style="margin-top:4px">Instructions
        <span style="font-size:10px;color:#444;font-weight:400;margin-left:6px">drafted from wiring · editable</span>
      </div>
      <textarea class="anode-instructions" id="anode-instr-${{idx}}"
                oninput="agentNodeState[${{idx}}].instructions=this.value"
                placeholder="Wire tools above to auto-draft, or write directly…">${{node.instructions||''}}</textarea>
    </div>`;
  }}).join('');
}}

function toggleNodeTool(idx, toolName) {{
  const node = agentNodeState[idx];
  const pos = node.tools.indexOf(toolName);
  if (pos === -1) node.tools.push(toolName);
  else node.tools.splice(pos, 1);
  // Re-render just the tools row
  const allTools = [...document.querySelectorAll('.tcard')].map(c => c.dataset.name).filter(Boolean);
  const row = document.getElementById(`anode-tools-${{idx}}`);
  if (row) row.innerHTML = node.tools.map(t =>
    `<span class="anode-tool assigned" onclick="toggleNodeTool(${{idx}},'${{t}}')" title="Click to remove">✓ ${{t}}</span>`
  ).join('') + allTools.filter(t => !node.tools.includes(t)).map(t =>
    `<span class="anode-tool" onclick="toggleNodeTool(${{idx}},'${{t}}')" title="Click to add">${{t}}</span>`
  ).join('');
}}

async function wireAgent(idx) {{
  const node = agentNodeState[idx];
  const behavior = (document.getElementById(`anode-behavior-${{idx}}`) || {{}}).value || node.behavior || '';
  if (!behavior.trim()) {{
    alert('Describe what this agent should do first.');
    return;
  }}
  node.behavior = behavior;
  const btn = document.getElementById(`anode-wire-${{idx}}`);
  const ta  = document.getElementById(`anode-instr-${{idx}}`);
  const toolsRow = document.getElementById(`anode-tools-${{idx}}`);
  if (btn) {{ btn.disabled = true; btn.textContent = 'Wiring…'; }}
  try {{
    const r = await fetch('/_apx/setup/wire-agent', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ behavior, agent_name: node.name }}),
    }});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Wire failed');
    // Update state
    node.tools = d.tools || [];
    node.instructions = d.instructions || '';
    if (ta) ta.value = node.instructions;
    // Re-render tools row
    const allTools = [...document.querySelectorAll('.tcard')].map(c => c.dataset.name).filter(Boolean);
    if (toolsRow) toolsRow.innerHTML = node.tools.map(t =>
      `<span class="anode-tool assigned" onclick="toggleNodeTool(${{idx}},'${{t}}')" title="Click to remove">✓ ${{t}}</span>`
    ).join('') + allTools.filter(t => !node.tools.includes(t)).map(t =>
      `<span class="anode-tool" onclick="toggleNodeTool(${{idx}},'${{t}}')" title="Click to add">${{t}}</span>`
    ).join('');
  }} catch(e) {{
    const ta2 = document.getElementById(`anode-instr-${{idx}}`);
    if (ta2 && !ta2.value) ta2.placeholder = `Wire failed: ${{e.message}}`;
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = 'Wire tools & draft ✦'; }}
  }}
}}

document.getElementById('btn-add-node').addEventListener('click', () => {{
  const name = prompt('Agent variable name (e.g. data_agent, response_agent):');
  if (!name || !/^[a-z_][a-z0-9_]*$/.test(name)) return;
  agentNodeState.push({{ name, tools: [], instructions: '', behavior: '', wrapper: null }});
  renderAgentNodes();
}});

document.getElementById('btn-apply-agents').addEventListener('click', async () => {{
  const btn = document.getElementById('btn-apply-agents');
  const st  = document.getElementById('agents-status');
  btn.disabled = true; st.style.color = '#60b0ff'; st.textContent = 'Applying…';
  try {{
    const r = await fetch('/_apx/setup/agents', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ nodes: agentNodeState }}),
    }});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Apply failed');
    st.style.color = '#4ade80'; st.textContent = '✓ Applied';
    await loadAgentNodes();
  }} catch(e) {{
    st.style.color = '#f87171'; st.textContent = e.message;
  }} finally {{ btn.disabled = false; }}
}});

// Load agent nodes on page load
loadAgentNodes();

// ── Probe ──
document.getElementById('btn-probe').addEventListener('click', async () => {{
  const url = document.getElementById('probe-url').value.trim();
  if (!url) return;
  const res = document.getElementById('probe-result');
  const btn = document.getElementById('btn-probe');
  btn.disabled = true; res.innerHTML = '<span style="color:#555;font-size:12px">Testing…</span>';
  try {{
    const r = await fetch('/_apx/setup/probe-json?url=' + encodeURIComponent(url));
    const d = await r.json();
    const ok = typeof d.status === 'number' && d.status < 400;
    const color = ok ? '#4ade80' : '#f87171';
    const rows = Object.entries(d).map(([k,v]) =>
      `<tr><td style="color:#666;font-family:monospace;font-size:11px;padding:3px 12px 3px 0;width:120px">${{k}}</td>`+
      `<td style="font-family:monospace;font-size:11px;color:#ccc;word-break:break-all">${{String(v)}}</td></tr>`
    ).join('');
    res.innerHTML = `<div style="background:#111;border:1px solid ${{ok?'#14532d':'#450a0a'}};border-radius:6px;padding:12px 14px">
      <div style="color:${{color}};font-size:13px;font-weight:600;margin-bottom:8px">${{ok?'✓':'✗'}} ${{d.status||d.error}}</div>
      <table style="border-collapse:collapse;width:100%">${{rows}}</table></div>`;
  }} catch(e) {{
    res.innerHTML = `<span style="color:#f87171;font-size:12px">${{e.message}}</span>`;
  }} finally {{ btn.disabled = false; }}
}});

// ── Vector Search (lazy) ──
let vsLoaded = false;
document.getElementById('vs-details').addEventListener('toggle', async (e) => {{
  if (!e.target.open || vsLoaded) return;
  vsLoaded = true;
  const el = document.getElementById('vs-content');
  el.innerHTML = '<span style="color:#555;font-size:12px">Loading…</span>';
  try {{
    const r = await fetch('/_apx/setup/vs-indexes');
    const indexes = await r.json();
    if (!indexes.length) {{ el.innerHTML = '<p style="color:#444;font-size:12px">No Vector Search indexes found.</p>'; return; }}
    el.innerHTML = indexes.map(idx => {{
      const ready = idx.ready;
      const snippet = `VS_INDEX = "${{idx.index}}"\nVS_COLUMNS = ${{JSON.stringify(idx.columns||[])}}`;
      return `<div style="background:#0d0d0d;border:1px solid #222;border-radius:6px;padding:12px 14px;margin-bottom:8px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
          <span style="font-family:monospace;font-size:12px;color:#e8e8e8">${{idx.index}}</span>
          <span style="font-size:10px;font-weight:600;padding:1px 6px;border-radius:8px;${{ready?'color:#4ade80;background:#052e16':'color:#f87171;background:#2a0a0a'}}">${{ready?'● Ready':'○ Not ready'}}</span>
        </div>
        ${{idx.source_table?`<div style="font-size:11px;color:#555;font-family:monospace;margin-bottom:8px">source: ${{idx.source_table}}</div>`:''}}
        <pre style="background:#111;border:1px solid #1e1e1e;border-radius:4px;padding:8px 10px;font-size:11px;color:#a5f3fc;overflow-x:auto">${{snippet}}</pre>
      </div>`;
    }}).join('');
  }} catch(e) {{
    el.innerHTML = `<p style="color:#f87171;font-size:12px">${{e.message}}</p>`;
  }}
}});

loadCatalogs();
loadWarehouses();
if (CUR_CATALOG && CUR_SCHEMA) loadTables(CUR_CATALOG, CUR_SCHEMA);

// Agent Pattern cards
function setActiveCard(type) {{
  document.querySelectorAll('.pcard').forEach(c => {{
    const p = c.dataset.pattern;
    c.classList.toggle('active', p === type || (type === 'LlmAgent' && p === 'Agent'));
  }});
}}

async function loadAgentPattern() {{
  try {{
    const r = await fetch('/_apx/setup/agent-pattern');
    const d = await r.json();
    setActiveCard(d.type || 'Agent');
  }} catch(e) {{}}
}}

document.querySelectorAll('.pcard').forEach(card => {{
  card.addEventListener('click', async () => {{
    const pattern = card.dataset.pattern;
    const isAuto = card.dataset.auto === '1';
    const status = document.getElementById('pattern-status');
    status.style.color = '#60b0ff';
    status.textContent = isAuto ? 'Applying…' : 'Loading snippet…';
    document.getElementById('pattern-snippet-wrap').style.display = 'none';
    try {{
      const r = await fetch('/_apx/setup/agent-pattern', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{pattern}}),
      }});
      const d = await r.json();
      if (!d.ok && !d.snippet) {{
        status.style.color = '#f87171';
        status.textContent = d.error || 'Failed';
        return;
      }}
      if (d.snippet) {{
        document.getElementById('pattern-snippet').textContent = d.snippet;
        document.getElementById('pattern-snippet-wrap').style.display = 'block';
        status.textContent = '';
      }} else {{
        setActiveCard(d.type);
        status.style.color = '#4ade80';
        status.textContent = d.changed ? 'Saved — hot-reload in progress' : `Already ${{d.type}}`;
        setTimeout(() => {{ status.textContent = ''; }}, 4000);
      }}
    }} catch(e) {{
      status.style.color = '#f87171';
      status.textContent = e.message;
    }}
  }});
}});

document.getElementById('btn-copy-snippet').addEventListener('click', () => {{
  const text = document.getElementById('pattern-snippet').textContent;
  navigator.clipboard.writeText(text).then(() => {{
    const btn = document.getElementById('btn-copy-snippet');
    btn.textContent = 'Copied!';
    setTimeout(() => {{ btn.textContent = 'Copy'; }}, 2000);
  }});
}});

loadAgentPattern();
</script>
{overlay}
</body>
</html>"""


def _render_eval_ui(eval_data: "list[dict[str, Any]]") -> str:
    """Eval page — run test questions through the agent and view responses."""
    import json as _json
    nav = _apx_nav_html("eval")
    overlay = _deploy_overlay_html()
    rows_json = _json.dumps(eval_data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Eval — APX Dev</title>
<style>
  * {{ box-sizing:border-box;margin:0;padding:0; }}
  body {{ background:#0d0d0d;color:#ccc;font-family:system-ui,sans-serif;font-size:13px; }}
  {_apx_nav_css()}
  .page {{ max-width:900px;margin:72px auto 40px;padding:0 20px; }}
  h2 {{ font-size:18px;font-weight:600;color:#fff;margin-bottom:4px; }}
  .subtitle {{ color:#555;margin-bottom:24px; }}
  .toolbar {{ display:flex;gap:8px;align-items:center;margin-bottom:16px; }}
  .btn-primary {{ background:#2563eb;color:#fff;border:none;border-radius:6px;padding:8px 18px;font-size:13px;font-weight:500;cursor:pointer; }}
  .btn-primary:hover {{ background:#1d4ed8; }}
  .btn-primary:disabled {{ opacity:.5;cursor:default; }}
  .btn-secondary {{ background:transparent;color:#888;border:1px solid #333;border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer; }}
  .btn-secondary:hover {{ color:#ccc;border-color:#555; }}
  #run-status {{ font-size:12px;color:#888; }}
  table {{ width:100%;border-collapse:collapse; }}
  th {{ text-align:left;padding:8px 12px;font-size:11px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid #1e1e1e; }}
  td {{ padding:10px 12px;border-bottom:1px solid #1a1a1a;vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  .q-cell {{ color:#ccc;max-width:280px; }}
  .exp-cell {{ color:#555;font-size:12px;max-width:200px; }}
  .resp-cell {{ font-size:12px;color:#888;max-width:360px; }}
  .status-cell {{ width:60px;text-align:center; }}
  .dot {{ width:10px;height:10px;border-radius:50%;display:inline-block; }}
  .dot-pass {{ background:#4ade80; }}
  .dot-fail {{ background:#f87171; }}
  .dot-pending {{ background:#333; }}
  .dot-running {{ background:#facc15;animation:pulse .8s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1}}50%{{opacity:.3}} }}
  .add-row {{ margin-top:12px; }}
  .add-row input, .add-row textarea {{ background:#111;border:1px solid #2a2a2a;color:#ccc;border-radius:6px;padding:7px 10px;font-size:12px;width:100%; }}
  .add-row textarea {{ resize:vertical;min-height:52px; }}
  .add-grid {{ display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px; }}
  .progress-bar {{ height:3px;background:#1e1e1e;border-radius:2px;margin-bottom:16px;overflow:hidden; }}
  .progress-fill {{ height:100%;background:#2563eb;border-radius:2px;transition:width .3s; }}
</style>
</head>
<body>
{nav}
<div class="page">
  <h2>Eval</h2>
  <p class="subtitle">Run test questions through your agent and review responses.</p>
  <div class="toolbar">
    <button class="btn-primary" id="btn-run-all">▶ Run All</button>
    <button class="btn-secondary" id="btn-clear">Clear Results</button>
    <span id="run-status"></span>
  </div>
  <div class="progress-bar"><div class="progress-fill" id="progress" style="width:0%"></div></div>
  <table>
    <thead><tr>
      <th>Question</th><th>Expected</th><th>Response</th><th>Pass</th>
    </tr></thead>
    <tbody id="eval-body"></tbody>
  </table>

  <div class="add-row" style="margin-top:20px">
    <div class="section-title" style="font-size:11px;font-weight:700;color:#555;text-transform:uppercase;letter-spacing:.4px;margin-bottom:8px">Add Test Case</div>
    <div class="add-grid">
      <textarea id="add-q" placeholder="Question…" rows="2"></textarea>
      <input id="add-exp" type="text" placeholder="Expected keywords (optional)">
    </div>
    <button class="btn-secondary" id="btn-add-case">+ Add</button>
  </div>
</div>

<script>
let rows = {rows_json};

function renderTable() {{
  const tbody = document.getElementById('eval-body');
  tbody.innerHTML = rows.map((r, i) => {{
    const statusCls = r.status === 'pass' ? 'dot-pass' : r.status === 'fail' ? 'dot-fail' :
                      r.status === 'running' ? 'dot-running' : 'dot-pending';
    return `<tr data-idx="${{i}}">
      <td class="q-cell">${{esc(r.question)}}</td>
      <td class="exp-cell">${{esc(r.expected || '—')}}</td>
      <td class="resp-cell" id="resp-${{i}}">${{esc(r.response || '')}}</td>
      <td class="status-cell"><span class="dot ${{statusCls}}"></span></td>
    </tr>`;
  }}).join('');
}}

function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

async function runCase(idx) {{
  const r = rows[idx];
  r.status = 'running'; r.response = '';
  renderTable();
  try {{
    const resp = await fetch('/invocations', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{input: [{{role: 'user', content: r.question}}]}}),
    }});
    const data = await resp.json();
    let text = '';
    try {{ text = data.output[0].content[0].text; }} catch {{ text = JSON.stringify(data); }}
    r.response = text;
    if (r.expected) {{
      const keywords = r.expected.split(/[,;]/).map(k => k.trim().toLowerCase()).filter(Boolean);
      r.status = keywords.every(k => text.toLowerCase().includes(k)) ? 'pass' : 'fail';
    }} else {{
      r.status = text.length > 10 ? 'pass' : 'fail';
    }}
  }} catch(e) {{
    r.response = 'Error: ' + e.message;
    r.status = 'fail';
  }}
  renderTable();
}}

document.getElementById('btn-run-all').addEventListener('click', async () => {{
  const btn = document.getElementById('btn-run-all');
  const status = document.getElementById('run-status');
  const progress = document.getElementById('progress');
  btn.disabled = true;
  for (let i = 0; i < rows.length; i++) {{
    status.textContent = `Running ${{i+1}}/${{rows.length}}…`;
    progress.style.width = (i / rows.length * 100) + '%';
    await runCase(i);
  }}
  progress.style.width = '100%';
  const passed = rows.filter(r => r.status === 'pass').length;
  status.textContent = `${{passed}}/${{rows.length}} passed`;
  btn.disabled = false;
}});

document.getElementById('btn-clear').addEventListener('click', () => {{
  rows.forEach(r => {{ r.status = 'pending'; r.response = ''; }});
  document.getElementById('run-status').textContent = '';
  document.getElementById('progress').style.width = '0%';
  renderTable();
}});

document.getElementById('btn-add-case').addEventListener('click', () => {{
  const q = document.getElementById('add-q').value.trim();
  if (!q) return;
  rows.push({{question: q, expected: document.getElementById('add-exp').value.trim(), status: 'pending', response: ''}});
  document.getElementById('add-q').value = '';
  document.getElementById('add-exp').value = '';
  renderTable();
}});

renderTable();
</script>
{overlay}
</body>
</html>"""


def _render_wizard_ui(current_env: "dict[str, str]") -> str:
    """First-run wizard: Connect → Explore → Generate Tools → Instructions → Launch."""
    nav = _apx_nav_html("wizard")
    nav_css = _apx_nav_css()
    prefill_catalog = current_env.get("DEMO_CATALOG", current_env.get("CATALOG", ""))
    prefill_schema = current_env.get("DEMO_SCHEMA", current_env.get("SCHEMA", ""))
    prefill_warehouse = current_env.get("WAREHOUSE_ID", "")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Setup Wizard — APX Dev</title>
<style>
  * {{ box-sizing:border-box;margin:0;padding:0; }}
  body {{ background:#0d0d0d;color:#ccc;font-family:system-ui,sans-serif;font-size:13px; }}
  {nav_css}
  .wiz-shell {{ max-width:680px;margin:72px auto 60px;padding:0 20px; }}

  /* Progress bar */
  .wiz-progress {{ display:flex;align-items:center;gap:0;margin-bottom:40px; }}
  .wiz-step-dot {{ display:flex;flex-direction:column;align-items:center;gap:6px;flex:1;position:relative; }}
  .wiz-step-dot:not(:last-child)::after {{
    content:"";position:absolute;top:14px;left:50%;width:100%;height:1px;background:#333;z-index:0;
  }}
  .dot {{ width:28px;height:28px;border-radius:50%;border:2px solid #333;background:#111;
          display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;
          color:#555;z-index:1;position:relative; }}
  .dot.active {{ border-color:#60b0ff;background:#0d1f38;color:#60b0ff; }}
  .dot.done {{ border-color:#22c55e;background:#052e16;color:#22c55e; }}
  .step-label {{ font-size:10px;color:#555;text-align:center;white-space:nowrap; }}
  .wiz-step-dot.s-active .step-label {{ color:#ccc; }}

  /* Step panels */
  .wiz-panel {{ display:none; }}
  .wiz-panel.visible {{ display:block; }}
  .wiz-panel h2 {{ font-size:20px;font-weight:700;color:#fff;margin-bottom:6px; }}
  .wiz-panel .sub {{ color:#666;margin-bottom:28px;line-height:1.6; }}

  /* Form elements */
  label {{ display:block;font-size:12px;color:#888;margin-bottom:4px;margin-top:14px; }}
  label:first-of-type {{ margin-top:0; }}
  select, input[type=text], textarea {{
    width:100%;background:#1a1a1a;border:1px solid #333;border-radius:6px;
    color:#ccc;padding:8px 10px;font-size:13px;font-family:inherit;
  }}
  select:disabled {{ opacity:.5; }}
  textarea {{ min-height:160px;resize:vertical;font-size:12px;line-height:1.6; }}

  /* Table explorer */
  .table-cards {{ display:flex;flex-direction:column;gap:10px; }}
  .tcard {{ background:#141414;border:1px solid #2a2a2a;border-radius:8px;padding:14px 16px; }}
  .tcard-header {{ display:flex;align-items:center;gap:10px;margin-bottom:8px; }}
  .tcard-name {{ font-weight:600;color:#fff;font-size:14px; }}
  .tcard-row-count {{ font-size:11px;color:#555;margin-left:auto; }}
  .tcard-cols {{ display:flex;flex-wrap:wrap;gap:5px; }}
  .col-chip {{ background:#1e1e1e;border:1px solid #2a2a2a;border-radius:4px;
               padding:2px 7px;font-size:11px;color:#888; }}
  .col-chip .ctype {{ color:#555; }}

  /* Tool proposals */
  .tool-proposals {{ display:flex;flex-direction:column;gap:8px; }}
  .tool-prop {{ background:#141414;border:1px solid #2a2a2a;border-radius:8px;padding:12px 14px;
                display:flex;align-items:flex-start;gap:12px; }}
  .tool-prop input[type=checkbox] {{ margin-top:2px;flex-shrink:0; }}
  .tool-prop-body {{ flex:1; }}
  .tool-prop-name {{ font-weight:600;color:#a78bfa;font-size:13px;margin-bottom:3px; }}
  .tool-prop-desc {{ color:#777;font-size:12px;line-height:1.5; }}
  #gen-progress {{ margin-top:16px;font-size:12px;color:#60b0ff;min-height:20px; }}

  /* Instructions step */
  .instr-preview {{ background:#141414;border:1px solid #2a2a2a;border-radius:8px;
                    padding:16px;font-size:12px;line-height:1.7;white-space:pre-wrap;
                    color:#ccc;margin-bottom:12px;max-height:280px;overflow-y:auto; }}

  /* Launch checklist */
  .checklist {{ display:flex;flex-direction:column;gap:10px;margin-bottom:28px; }}
  .check-item {{ display:flex;align-items:center;gap:10px;font-size:13px; }}
  .check-item .ck {{ width:20px;height:20px;border-radius:50%;display:flex;align-items:center;
                     justify-content:center;font-size:12px;flex-shrink:0; }}
  .ck.ok {{ background:#052e16;border:1px solid #22c55e;color:#22c55e; }}
  .ck.warn {{ background:#2a1a00;border:1px solid #5a3a00;color:#f59e0b; }}

  /* Footer nav */
  .wiz-footer {{ display:flex;align-items:center;justify-content:space-between;
                 margin-top:32px;padding-top:20px;border-top:1px solid #1e1e1e; }}
  .btn {{ padding:9px 20px;border-radius:6px;font-size:13px;font-weight:600;
          cursor:pointer;border:1px solid transparent; }}
  .btn-primary {{ background:#1e3a5f;color:#60b0ff;border-color:#2a5298; }}
  .btn-primary:hover {{ background:#2a4f7a; }}
  .btn-primary:disabled {{ opacity:.45;cursor:default; }}
  .btn-ghost {{ background:transparent;color:#555;border-color:#333; }}
  .btn-ghost:hover {{ color:#ccc;border-color:#444; }}
  .btn-success {{ background:#052e16;color:#22c55e;border-color:#166534; }}
  .btn-success:hover {{ background:#0a4a22; }}
  .err {{ color:#f87171;font-size:12px;margin-top:8px; }}
  .spinner {{ display:inline-block;width:12px;height:12px;border:2px solid #333;
              border-top-color:#60b0ff;border-radius:50%;animation:spin .7s linear infinite; }}
  @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
</style>
</head>
<body>
{nav}
<div class="wiz-shell">

  <!-- Progress indicator -->
  <div class="wiz-progress" id="progress-bar">
    <div class="wiz-step-dot s-active" id="pd-1"><div class="dot active">1</div><div class="step-label">Connect</div></div>
    <div class="wiz-step-dot" id="pd-2"><div class="dot">2</div><div class="step-label">Explore</div></div>
    <div class="wiz-step-dot" id="pd-3"><div class="dot">3</div><div class="step-label">Tools</div></div>
    <div class="wiz-step-dot" id="pd-4"><div class="dot">4</div><div class="step-label">Instructions</div></div>
    <div class="wiz-step-dot" id="pd-5"><div class="dot">5</div><div class="step-label">Launch</div></div>
  </div>

  <!-- Step 1: Connect -->
  <div class="wiz-panel visible" id="step-1">
    <h2>Connect to your data</h2>
    <p class="sub">Choose the Unity Catalog schema and warehouse that contain your data. These will be saved to your <code>.env</code> file.</p>

    <label>Catalog</label>
    <select id="w-catalog"><option value="">Loading…</option></select>

    <label>Schema</label>
    <select id="w-schema" disabled><option value="">Select a catalog first</option></select>

    <label>SQL Warehouse</label>
    <select id="w-warehouse" disabled><option value="">Select a catalog first</option></select>

    <div id="s1-err" class="err"></div>
    <div class="wiz-footer">
      <span></span>
      <button class="btn btn-primary" id="s1-next" disabled>Next →</button>
    </div>
  </div>

  <!-- Step 2: Explore -->
  <div class="wiz-panel" id="step-2">
    <h2>Your data</h2>
    <p class="sub">Here are the tables in the schema you selected. Review them — you'll generate tools for these next.</p>
    <div class="table-cards" id="table-cards"><p style="color:#555">Loading tables…</p></div>
    <div id="s2-err" class="err"></div>
    <div class="wiz-footer">
      <button class="btn btn-ghost" id="s2-back">← Back</button>
      <button class="btn btn-primary" id="s2-next">Next →</button>
    </div>
  </div>

  <!-- Step 3: Generate Tools -->
  <div class="wiz-panel" id="step-3">
    <h2>Generate tools</h2>
    <p class="sub">Select which tables to build tools for. The agent will be able to query each one.</p>
    <div class="tool-proposals" id="tool-proposals"></div>
    <div id="gen-progress"></div>
    <div id="s3-err" class="err"></div>
    <div class="wiz-footer">
      <button class="btn btn-ghost" id="s3-back">← Back</button>
      <button class="btn btn-primary" id="s3-next">Generate →</button>
    </div>
  </div>

  <!-- Step 4: Instructions -->
  <div class="wiz-panel" id="step-4">
    <h2>Agent instructions</h2>
    <p class="sub">These instructions define how your agent behaves. They've been generated from your schema — edit as needed, then apply.</p>
    <div id="instr-loading" style="color:#555">Generating instructions…  <span class="spinner"></span></div>
    <textarea id="instr-text" style="display:none"></textarea>
    <div id="s4-err" class="err"></div>
    <div class="wiz-footer">
      <button class="btn btn-ghost" id="s4-back">← Back</button>
      <button class="btn btn-primary" id="s4-next">Apply &amp; Continue →</button>
    </div>
  </div>

  <!-- Step 5: Launch -->
  <div class="wiz-panel" id="step-5">
    <h2>You're ready</h2>
    <p class="sub">Your agent is configured. Here's a summary of what was set up.</p>
    <div class="checklist" id="launch-checklist"></div>
    <div class="wiz-footer">
      <button class="btn btn-ghost" id="s5-back">← Back</button>
      <a href="/_apx/agent" class="btn btn-success">Open Chat →</a>
    </div>
  </div>

</div>
<script>
(function() {{
  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  const state = {{
    catalog: '{prefill_catalog}',
    schema: '{prefill_schema}',
    warehouseId: '{prefill_warehouse}',
    warehouseName: '',
    tables: [],        // [{{name, columns: [{{name, type}}], row_count}}]
    tools: [],         // [{{name, description, selected}}]
    instructions: '',
    toolsCreated: 0,
  }};

  // ---------------------------------------------------------------------------
  // Step navigation
  // ---------------------------------------------------------------------------
  let currentStep = 1;
  const TOTAL = 5;

  function showStep(n) {{
    for (let i = 1; i <= TOTAL; i++) {{
      document.getElementById(`step-${{i}}`).classList.toggle('visible', i === n);
      const pd = document.getElementById(`pd-${{i}}`);
      const dot = pd.querySelector('.dot');
      pd.classList.toggle('s-active', i === n);
      dot.classList.toggle('active', i === n);
      dot.classList.toggle('done', i < n);
    }}
    currentStep = n;
  }}

  // ---------------------------------------------------------------------------
  // Step 1: Connect
  // ---------------------------------------------------------------------------
  const wCatalog = document.getElementById('w-catalog');
  const wSchema  = document.getElementById('w-schema');
  const wWh      = document.getElementById('w-warehouse');
  const s1Next   = document.getElementById('s1-next');
  const s1Err    = document.getElementById('s1-err');

  function checkS1Ready() {{
    s1Next.disabled = !(state.catalog && state.schema && state.warehouseId);
  }}

  async function loadCatalogs() {{
    try {{
      const r = await fetch('/_apx/setup/catalogs');
      const cats = await r.json();
      wCatalog.innerHTML = '<option value="">Select catalog…</option>' +
        cats.map(c => `<option${{c===state.catalog?' selected':''}}>${{c}}</option>`).join('');
      if (state.catalog) {{ await loadSchemas(state.catalog); }}
    }} catch(e) {{ s1Err.textContent = 'Failed to load catalogs: ' + e.message; }}
  }}

  async function loadSchemas(catalog) {{
    wSchema.disabled = true;
    wSchema.innerHTML = '<option>Loading…</option>';
    try {{
      const r = await fetch('/_apx/setup/schemas?catalog=' + encodeURIComponent(catalog));
      const schemas = await r.json();
      wSchema.innerHTML = '<option value="">Select schema…</option>' +
        schemas.map(s => `<option${{s===state.schema?' selected':''}}>${{s}}</option>`).join('');
      wSchema.disabled = false;
      if (state.schema) checkS1Ready();
    }} catch(e) {{ s1Err.textContent = 'Failed to load schemas: ' + e.message; }}
  }}

  async function loadWarehouses() {{
    wWh.disabled = true;
    wWh.innerHTML = '<option>Loading…</option>';
    try {{
      const r = await fetch('/_apx/setup/warehouses');
      const whs = await r.json();
      wWh.innerHTML = '<option value="">Select warehouse…</option>' +
        whs.map(w => `<option value="${{w.id}}"${{w.id===state.warehouseId?' selected':''}}>${{w.name}} (${{w.state}})</option>`).join('');
      wWh.disabled = false;
      if (state.warehouseId) checkS1Ready();
    }} catch(e) {{ s1Err.textContent = 'Failed to load warehouses: ' + e.message; }}
  }}

  wCatalog.addEventListener('change', () => {{
    state.catalog = wCatalog.value;
    state.schema = '';
    state.warehouseId = '';
    wSchema.innerHTML = '<option value="">Select schema…</option>';
    wWh.innerHTML = '<option value="">Select warehouse…</option>';
    wWh.disabled = true;
    if (state.catalog) {{ loadSchemas(state.catalog); loadWarehouses(); }}
    checkS1Ready();
  }});

  wSchema.addEventListener('change', () => {{
    state.schema = wSchema.value;
    checkS1Ready();
  }});

  wWh.addEventListener('change', () => {{
    state.warehouseId = wWh.value;
    state.warehouseName = wWh.options[wWh.selectedIndex]?.text || '';
    checkS1Ready();
  }});

  s1Next.addEventListener('click', async () => {{
    s1Next.disabled = true;
    s1Next.textContent = 'Saving…';
    s1Err.textContent = '';
    try {{
      const r = await fetch('/_apx/setup', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{
          catalog: state.catalog,
          schema: state.schema,
          warehouse_id: state.warehouseId,
        }})
      }});
      const d = await r.json();
      if (!r.ok) {{ throw new Error(d.detail || 'Save failed'); }}
      showStep(2);
      loadTables();
    }} catch(e) {{
      s1Err.textContent = e.message;
      s1Next.disabled = false;
      s1Next.textContent = 'Next →';
    }}
  }});

  // ---------------------------------------------------------------------------
  // Step 2: Explore tables
  // ---------------------------------------------------------------------------
  const tableCards = document.getElementById('table-cards');
  const s2Err = document.getElementById('s2-err');

  async function loadTables() {{
    tableCards.innerHTML = '<p style="color:#555">Loading tables… <span class="spinner"></span></p>';
    try {{
      const r = await fetch(`/_apx/wizard/tables?catalog=${{encodeURIComponent(state.catalog)}}&schema=${{encodeURIComponent(state.schema)}}`);
      const tables = await r.json();
      state.tables = tables;
      if (!tables.length) {{
        tableCards.innerHTML = '<p style="color:#666">No tables found in this schema.</p>';
        return;
      }}
      tableCards.innerHTML = tables.map(t => `
        <div class="tcard">
          <div class="tcard-header">
            <span class="tcard-name">${{t.name}}</span>
            <span class="tcard-row-count">${{t.row_count != null ? t.row_count.toLocaleString() + ' rows' : ''}}</span>
          </div>
          <div class="tcard-cols">
            ${{t.columns.map(c => `<span class="col-chip">${{c.name}} <span class="ctype">${{c.type}}</span></span>`).join('')}}
          </div>
        </div>`).join('');
      // Build tool proposals for step 3
      buildProposals(tables);
    }} catch(e) {{
      s2Err.textContent = 'Failed to load tables: ' + e.message;
      tableCards.innerHTML = '';
    }}
  }}

  document.getElementById('s2-back').addEventListener('click', () => showStep(1));
  document.getElementById('s2-next').addEventListener('click', () => showStep(3));

  // ---------------------------------------------------------------------------
  // Step 3: Generate tools
  // ---------------------------------------------------------------------------
  const toolProposals = document.getElementById('tool-proposals');
  const genProgress   = document.getElementById('gen-progress');
  const s3Err         = document.getElementById('s3-err');

  function buildProposals(tables) {{
    state.tools = tables.map(t => ({{
      name: t.name,
      description: `Query the ${{t.name}} table. Columns: ${{t.columns.map(c=>c.name).join(', ')}}.`,
      selected: true,
    }}));
    toolProposals.innerHTML = state.tools.map((t, i) => `
      <div class="tool-prop">
        <input type="checkbox" id="tp-${{i}}" data-idx="${{i}}" checked>
        <div class="tool-prop-body">
          <div class="tool-prop-name">${{t.name}}</div>
          <div class="tool-prop-desc">${{t.description}}</div>
        </div>
      </div>`).join('');
    toolProposals.querySelectorAll('input[type=checkbox]').forEach(cb => {{
      cb.addEventListener('change', () => {{
        state.tools[+cb.dataset.idx].selected = cb.checked;
      }});
    }});
  }}

  document.getElementById('s3-back').addEventListener('click', () => showStep(2));

  document.getElementById('s3-next').addEventListener('click', async () => {{
    const selected = state.tools.filter(t => t.selected);
    if (!selected.length) {{ s3Err.textContent = 'Select at least one table.'; return; }}
    s3Err.textContent = '';
    const btn = document.getElementById('s3-next');
    btn.disabled = true;
    genProgress.innerHTML = '';
    state.toolsCreated = 0;

    for (const t of selected) {{
      genProgress.innerHTML += `<div><span class="spinner"></span> Generating tool for <strong>${{t.name}}</strong>…</div>`;
      try {{
        const r = await fetch('/_apx/wizard/generate-tools', {{
          method: 'POST',
          headers: {{'Content-Type':'application/json'}},
          body: JSON.stringify({{ table: t.name, description: t.description,
                                  catalog: state.catalog, schema: state.schema,
                                  warehouse_id: state.warehouseId }})
        }});
        const d = await r.json();
        genProgress.lastElementChild.innerHTML =
          `<span style="color:#22c55e">✓</span> <strong>${{t.name}}</strong>: ${{d.tool_name || 'created'}}`;
        state.toolsCreated++;
      }} catch(e) {{
        genProgress.lastElementChild.innerHTML =
          `<span style="color:#f87171">✗</span> <strong>${{t.name}}</strong>: ${{e.message}}`;
      }}
    }}
    btn.disabled = false;
    btn.textContent = 'Next →';
    setTimeout(() => {{ showStep(4); loadInstructions(); }}, 600);
  }});

  // ---------------------------------------------------------------------------
  // Step 4: Instructions
  // ---------------------------------------------------------------------------
  const instrLoading = document.getElementById('instr-loading');
  const instrText    = document.getElementById('instr-text');
  const s4Err        = document.getElementById('s4-err');

  async function loadInstructions() {{
    instrLoading.style.display = '';
    instrText.style.display = 'none';
    s4Err.textContent = '';
    try {{
      const r = await fetch('/_apx/setup/generate-instructions', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{
          catalog: state.catalog, schema: state.schema, warehouse_id: state.warehouseId
        }})
      }});
      const d = await r.json();
      state.instructions = d.instructions || '';
      instrText.value = state.instructions;
      instrLoading.style.display = 'none';
      instrText.style.display = '';
    }} catch(e) {{
      s4Err.textContent = 'Failed to generate instructions: ' + e.message;
      instrLoading.style.display = 'none';
    }}
  }}

  document.getElementById('s4-back').addEventListener('click', () => showStep(3));

  document.getElementById('s4-next').addEventListener('click', async () => {{
    const btn = document.getElementById('s4-next');
    btn.disabled = true;
    btn.textContent = 'Applying…';
    s4Err.textContent = '';
    try {{
      const r = await fetch('/_apx/setup/apply-instructions', {{
        method: 'POST',
        headers: {{'Content-Type':'application/json'}},
        body: JSON.stringify({{ instructions: instrText.value }})
      }});
      if (!r.ok) {{ throw new Error(await r.text()); }}
      buildLaunchChecklist();
      showStep(5);
    }} catch(e) {{
      s4Err.textContent = e.message;
    }} finally {{
      btn.disabled = false;
      btn.textContent = 'Apply & Continue →';
    }}
  }});

  // ---------------------------------------------------------------------------
  // Step 5: Launch
  // ---------------------------------------------------------------------------
  function buildLaunchChecklist() {{
    const items = [
      [true,  `Catalog: <strong>${{state.catalog}}</strong>`],
      [true,  `Schema: <strong>${{state.schema}}</strong>`],
      [!!state.warehouseId, `Warehouse: <strong>${{state.warehouseName || state.warehouseId}}</strong>`],
      [state.toolsCreated > 0, `${{state.toolsCreated}} tool${{state.toolsCreated!==1?'s':''}} generated`],
      [!!instrText.value.trim(), 'Agent instructions set'],
    ];
    document.getElementById('launch-checklist').innerHTML = items.map(([ok, label]) => `
      <div class="check-item">
        <div class="ck ${{ok?'ok':'warn'}}">${{ok?'✓':'!'}}</div>
        <span>${{label}}</span>
      </div>`).join('');
  }}

  document.getElementById('s5-back').addEventListener('click', () => showStep(4));

  // ---------------------------------------------------------------------------
  // Boot: load catalogs + warehouses in parallel
  // ---------------------------------------------------------------------------
  loadCatalogs();
  loadWarehouses();
}})();
</script>
</body>
</html>"""


