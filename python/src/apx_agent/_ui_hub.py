"""Dev UI — /_apx/hub agent registry with register/refresh/invoke/deregister."""

from __future__ import annotations

from ._ui_nav import _apx_nav_css, _apx_nav_html
from ._ui_theme import apx_theme_style


def render_hub_ui() -> str:
    """Render Hub: list caller's registered agents with register/refresh/invoke/delete actions."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Hub · APX dev</title>
  {apx_theme_style()}
  <style>
    {_apx_nav_css()}
    :root {{
      --bg:#11171C; --panel:#1F272D; --border:#445461; --text:#E8ECF0;
      --muted:#92A4B3; --accent:#2272B4; --accent-bg:#37444F; --accent-border:#445461;
      --ok:#3BA65E; --ok-bg:#37444F; --err:#E74C3C;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text);
           font:13px/1.45 ui-sans-serif,system-ui,-apple-system,sans-serif; }}
    main {{ max-width:960px; margin:0 auto; padding:64px 20px 48px; }}
    h1 {{ font-size:18px; margin:0 0 4px; }}
    .sub {{ color:var(--muted); margin:0 0 20px; font-size:12px; }}
    .row {{ display:flex; gap:8px; align-items:flex-end; flex-wrap:wrap; margin-bottom:14px; }}
    .row label {{ font-size:12px; color:var(--muted); }}
    button, .btn {{ font:inherit; cursor:pointer; color:var(--accent); background:var(--accent-bg);
                    border:1px solid var(--accent-border); border-radius:6px; padding:6px 12px; }}
    button:disabled {{ opacity:.5; cursor:default; }}
    button.secondary {{ color:var(--text); background:var(--panel); border-color:var(--border); }}
    button.danger {{ color:#E74C3C; background:var(--panel); border-color:#E74C3C; }}
    input {{ font:inherit; background:var(--bg); color:var(--text);
            border:1px solid var(--border); border-radius:6px; padding:6px 10px; }}
    input:focus {{ outline:none; border-color:var(--accent); }}
    .card {{ background:var(--panel); border:1px solid var(--border); border-radius:8px;
             padding:12px 14px; margin-bottom:8px; }}
    .card h3 {{ margin:0 0 4px; font-size:13px; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
    .card-status {{ font-size:11px; padding:2px 6px; border-radius:4px; }}
    .card-status.live {{ color:#3BA65E; background:#052e1c; }}
    .card-status.unreachable {{ color:#E74C3C; background:#3d1a1a; }}
    .card-status.unknown {{ color:var(--muted); background:var(--border); }}
    .card-desc {{ color:var(--muted); font-size:12px; margin:4px 0 8px; }}
    .card-meta {{ font-size:11px; color:var(--muted); font-family:var(--apx-mono); margin:4px 0; }}
    .card-tools {{ display:flex; flex-wrap:wrap; gap:4px; margin:8px 0; }}
    .card-tool {{ font-size:10px; background:var(--bg); border:1px solid var(--border);
                  border-radius:4px; padding:2px 6px; color:var(--text); }}
    .card-actions {{ display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-top:10px; }}
    .empty {{ color:var(--muted); padding:16px 0; font-style:italic; }}
    .err {{ color:#E74C3C; font-size:12px; margin-top:6px; }}
    .success {{ color:#3BA65E; font-size:12px; margin-top:6px; }}
    .hint {{ color:var(--muted); font-size:11px; margin-top:4px; }}
    a {{ color:var(--accent); text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    section {{ margin-bottom:28px; }}
    section h2 {{ font-size:14px; margin:0 0 10px; }}
    #register-section {{ background:var(--panel); border:1px solid var(--border);
                        border-radius:8px; padding:14px; margin-bottom:20px; }}
    #register-section .form-group {{ margin-bottom:12px; }}
    #register-section label {{ display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }}
    #register-section input {{ width:100%; max-width:500px; }}
    #agents-section {{ margin-top:20px; }}
    #agents-loading {{ color:var(--muted); }}
  </style>
</head>
<body>
{_apx_nav_html("hub")}
<main>
  <h1>Hub</h1>
  <p class="sub">Register agents by URL, refresh their status, and invoke them with OBO-scoped identity.</p>

  <section id="register-section">
    <h2>Register Agent</h2>
    <div class="form-group">
      <label for="register-url">Agent URL</label>
      <input type="url" id="register-url" placeholder="https://agent.databricksapps.com"
             pattern="https://.*databricksapps\\.com.*" />
      <div class="hint">Must be an HTTPS URL on *.databricksapps.com</div>
    </div>
    <div class="form-group">
      <label for="register-tags">Tags (comma-separated, optional)</label>
      <input type="text" id="register-tags" placeholder="e.g., production, team-x" />
    </div>
    <div class="row">
      <button id="btn-register" onclick="registerAgent()">Register</button>
      <span id="register-status"></span>
    </div>
  </section>

  <section id="agents-section">
    <h2>Your Agents</h2>
    <div id="agents-list"></div>
  </section>
</main>

<script>
// Fetch auth headers from the shell (forwarded headers set by the dev-UI frame)
function getAuthHeaders() {{
  const token = document.querySelector('meta[name="apx-token"]')?.content;
  const user = document.querySelector('meta[name="apx-user"]')?.content;
  const headers = {{'Content-Type': 'application/json'}};
  if (token) headers['X-Forwarded-Access-Token'] = token;
  if (user) headers['X-Forwarded-User'] = user;
  return headers;
}}

// Resolve API base URL (dev local vs deployed app)
function getApiBase() {{
  if (window.self !== window.top) {{
    // Inside iframe — routes are relative to the app root
    return '/_apx';
  }}
  return location.origin + '/_apx';
}}

async function loadAgents() {{
  const list = document.getElementById('agents-list');
  list.innerHTML = '<div id="agents-loading">Loading...</div>';
  try {{
    const res = await fetch(getApiBase() + '/hub/agents', {{
      headers: getAuthHeaders()
    }});
    if (res.status === 401) {{
      list.innerHTML = '<div class="err">Error: Not authorized (401). Make sure you are logged in.</div>';
      return;
    }}
    if (!res.ok) {{
      list.innerHTML = `<div class="err">Error loading agents: ${{res.status}} ${{res.statusText}}</div>`;
      return;
    }}
    const data = await res.json();
    renderAgents(data);
  }} catch (err) {{
    list.innerHTML = `<div class="err">Error: ${{err.message}}</div>`;
  }}
}}

function renderAgents(data) {{
  const list = document.getElementById('agents-list');
  const agents = data || [];
  if (agents.length === 0) {{
    list.innerHTML = '<div class="empty">No agents registered yet.</div>';
    return;
  }}
  list.innerHTML = agents.map(agent => `
    <div class="card">
      <h3>
        ${{agent.display_name || agent.url}}
        <span class="card-status ${{agent.status}}">${{agent.status}}</span>
      </h3>
      <div class="card-meta">
        <div>ID: <code>${{agent.id}}</code></div>
        <div>URL: <a href="${{agent.url}}" target="_blank">${{agent.url}}</a></div>
      </div>
      ${{agent.description ? `<div class="card-desc">${{agent.description}}</div>` : ''}}
      ${{agent.tools && agent.tools.length > 0 ? `
        <div class="card-tools">
          ${{agent.tools.map(t => `<span class="card-tool">${{t.name || t}}</span>`).join('')}}
        </div>
      ` : ''}}
      <div class="card-actions">
        <button class="secondary" onclick="refreshAgent('${{agent.id}}')">Refresh</button>
        <button class="secondary" onclick="showInvokeModal('${{agent.id}}', '${{agent.display_name || agent.url}}')">Invoke</button>
        <button class="danger secondary" onclick="deleteAgent('${{agent.id}}')">Delete</button>
      </div>
      <div id="status-${{agent.id}}"></div>
    </div>
  `).join('');
}}

async function registerAgent() {{
  const url = document.getElementById('register-url').value.trim();
  const tags = document.getElementById('register-tags').value.trim()
    .split(',').map(t => t.trim()).filter(t => t);
  const status = document.getElementById('register-status');

  if (!url) {{
    status.innerHTML = '<div class="err">Please enter an agent URL</div>';
    return;
  }}

  status.innerHTML = '<div style="color:var(--muted);">Registering...</div>';
  try {{
    const res = await fetch(getApiBase() + '/hub/agents', {{
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({{ url, tags }})
    }});

    if (res.status === 401) {{
      status.innerHTML = '<div class="err">Error: Not authorized (401)</div>';
      return;
    }}
    if (!res.ok) {{
      const err = await res.text();
      status.innerHTML = `<div class="err">Error: ${{res.status}} ${{err}}</div>`;
      return;
    }}

    const agent = await res.json();
    status.innerHTML = `<div class="success">Registered! ID: ${{agent.id}}</div>`;
    document.getElementById('register-url').value = '';
    document.getElementById('register-tags').value = '';
    setTimeout(loadAgents, 500);
  }} catch (err) {{
    status.innerHTML = `<div class="err">Error: ${{err.message}}</div>`;
  }}
}}

async function refreshAgent(agentId) {{
  const statusEl = document.getElementById('status-' + agentId);
  statusEl.innerHTML = '<div style="color:var(--muted); font-size:11px;">Refreshing...</div>';
  try {{
    const res = await fetch(getApiBase() + '/hub/agents/' + encodeURIComponent(agentId) + '/refresh', {{
      method: 'POST',
      headers: getAuthHeaders()
    }});

    if (!res.ok) {{
      statusEl.innerHTML = `<div class="err">Error: ${{res.status}}</div>`;
      return;
    }}

    const agent = await res.json();
    statusEl.innerHTML = `<div class="success">Refreshed. Status: ${{agent.status}}</div>`;
    setTimeout(loadAgents, 500);
  }} catch (err) {{
    statusEl.innerHTML = `<div class="err">Error: ${{err.message}}</div>`;
  }}
}}

async function deleteAgent(agentId) {{
  if (!confirm('Delete this agent registration?')) return;
  const statusEl = document.getElementById('status-' + agentId);
  statusEl.innerHTML = '<div style="color:var(--muted); font-size:11px;">Deleting...</div>';
  try {{
    const res = await fetch(getApiBase() + '/hub/agents/' + encodeURIComponent(agentId), {{
      method: 'DELETE',
      headers: getAuthHeaders()
    }});

    if (!res.ok) {{
      statusEl.innerHTML = `<div class="err">Error: ${{res.status}}</div>`;
      return;
    }}

    statusEl.innerHTML = '<div class="success">Deleted</div>';
    setTimeout(loadAgents, 500);
  }} catch (err) {{
    statusEl.innerHTML = `<div class="err">Error: ${{err.message}}</div>`;
  }}
}}

function showInvokeModal(agentId, displayName) {{
  const input = prompt(`Invoke ${{displayName}}\\n\\nEnter JSON input:`, '{{}}');
  if (input === null) return;
  invokeAgent(agentId, input);
}}

async function invokeAgent(agentId, input) {{
  try {{
    let parsedInput;
    try {{
      parsedInput = JSON.parse(input);
    }} catch {{
      alert('Invalid JSON input');
      return;
    }}

    const res = await fetch(getApiBase() + '/hub/agents/' + encodeURIComponent(agentId) + '/invoke', {{
      method: 'POST',
      headers: getAuthHeaders(),
      body: JSON.stringify({{ input: parsedInput }})
    }});

    if (!res.ok) {{
      alert(`Error: ${{res.status}} ${{res.statusText}}`);
      return;
    }}

    const result = await res.json();
    alert(`Response:\\n\\n${{JSON.stringify(result, null, 2)}}`);
  }} catch (err) {{
    alert(`Error: ${{err.message}}`);
  }}
}}

// Load agents on page load
window.addEventListener('load', loadAgents);
</script>
</body>
</html>
"""
