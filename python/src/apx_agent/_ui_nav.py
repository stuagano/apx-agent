"""Shared navigation bar CSS/HTML and deploy overlay."""

from __future__ import annotations

def _apx_nav_css() -> str:
    return """
  #apx-header { position:fixed;top:0;left:0;right:0;z-index:1000;background:#111;border-bottom:1px solid #2a2a2a; }
  #apx-nav { padding:10px 16px;display:flex;align-items:center;gap:10px;height:44px; }
  .badge { background:#1e3a5f;color:#60b0ff;font-size:11px;font-weight:600;padding:2px 8px;border-radius:4px;letter-spacing:.5px;text-transform:uppercase; }
  nav { margin-left:auto;display:flex;gap:4px; }
  nav a { font-size:12px;color:#888;text-decoration:none;padding:3px 10px;border-radius:5px;border:1px solid transparent; }
  nav a:hover { color:#ccc;border-color:#333; }
  nav a.active { color:#60b0ff;background:#0d1f38;border-color:#1e3a5f; }
"""


def _apx_nav_html(active: str) -> str:
    pages = [("agent", "Chat"), ("edit", "Edit"), ("setup", "Setup")]
    active_cls = 'class="active"'
    links = "".join(
        f'<a href="/_apx/{p}" {active_cls if p == active else ""}>{label}</a>'
        for p, label in pages
    )
    return f"""<div id="apx-header"><div id="apx-nav">
  <span class="badge">APX dev</span>
  <nav>{links}</nav>
</div></div>"""


def _deploy_overlay_html() -> str:
    """Shared deploy modal + SSE log viewer injected into every /_apx/ page."""
    return """
<style>
  #btn-deploy { background: #1a1040; color: #a78bfa; border: 1px solid #4c1d95;
                border-radius: 6px; padding: 5px 14px; font-size: 12px; font-weight: 600;
                cursor: pointer; white-space: nowrap; }
  #btn-deploy:hover { background: #2d1b69; }
  #btn-deploy:disabled { opacity: .5; cursor: default; }
  #deploy-overlay { display: none; position: fixed; inset: 0; z-index: 2000;
                    background: rgba(0,0,0,.75); align-items: center; justify-content: center; }
  #deploy-overlay.open { display: flex; }
  #deploy-modal { background: #111; border: 1px solid #2a2a2a; border-radius: 10px;
                  width: min(700px, 95vw); max-height: 80vh; display: flex;
                  flex-direction: column; overflow: hidden; }
  #deploy-modal-head { padding: 12px 16px; border-bottom: 1px solid #1e1e1e;
                       display: flex; align-items: center; justify-content: space-between; }
  #deploy-modal-head h2 { font-size: 13px; font-weight: 600; color: #ccc; }
  #deploy-modal-close { background: none; border: none; color: #555; font-size: 18px;
                        cursor: pointer; padding: 2px 6px; }
  #deploy-modal-close:hover { color: #ccc; }
  #deploy-log { flex: 1; overflow-y: auto; padding: 12px 16px;
                font-family: monospace; font-size: 11px; line-height: 1.6;
                color: #aaa; white-space: pre-wrap; word-break: break-all; }
  #deploy-log .log-err { color: #f87171; }
  #deploy-log .log-ok { color: #4ade80; }
  #deploy-log .log-dim { color: #555; }
  #deploy-foot { padding: 10px 16px; border-top: 1px solid #1e1e1e;
                 display: flex; align-items: center; gap: 10px; }
  #deploy-status { flex: 1; font-size: 12px; color: #666; }
  #deploy-status.ok { color: #4ade80; }
  #deploy-status.err { color: #f87171; }
  #deploy-close-btn { background: transparent; color: #888; border: 1px solid #333;
                      border-radius: 6px; padding: 5px 14px; font-size: 12px; cursor: pointer; }
  #deploy-close-btn:hover { color: #ccc; border-color: #555; }
</style>

<div id="deploy-overlay">
  <div id="deploy-modal">
    <div id="deploy-modal-head">
      <h2>Deploy to Databricks</h2>
      <button id="deploy-modal-close">✕</button>
    </div>
    <div id="deploy-log"></div>
    <div id="deploy-foot">
      <span id="deploy-status">Starting…</span>
      <button id="deploy-close-btn" style="display:none">Close</button>
    </div>
  </div>
</div>

<script>
(function() {
  const btn = document.getElementById('btn-deploy');
  const overlay = document.getElementById('deploy-overlay');
  const log = document.getElementById('deploy-log');
  const status = document.getElementById('deploy-status');
  const closeBtn = document.getElementById('deploy-close-btn');

  function appendLog(text) {
    const line = document.createElement('span');
    // Colour hints
    if (/error|failed|exception/i.test(text)) line.className = 'log-err';
    else if (/success|deployed|complete|✓|done/i.test(text)) line.className = 'log-ok';
    else if (/^\\s*$/.test(text)) line.className = 'log-dim';
    line.textContent = text + '\\n';
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  }

  function startDeploy() {
    log.innerHTML = '';
    status.textContent = 'Deploying…';
    status.className = '';
    closeBtn.style.display = 'none';
    btn.disabled = true;
    overlay.classList.add('open');

    const es = new EventSource('/_apx/deploy/stream');

    es.onmessage = (e) => {
      const msg = e.data;
      if (msg.startsWith('__EXIT__')) {
        es.close();
        const code = parseInt(msg.replace('__EXIT__', ''), 10);
        if (code === 0) {
          status.textContent = '✓ Deployed — app is restarting…';
          status.className = 'ok';
          // Poll health until the app comes back up
          pollHealth();
        } else {
          status.textContent = `✗ Deploy failed (exit ${code})`;
          status.className = 'err';
          closeBtn.style.display = '';
          btn.disabled = false;
        }
      } else if (msg.startsWith('__ERROR__')) {
        es.close();
        appendLog(msg.replace('__ERROR__', ''));
        status.textContent = '✗ Error';
        status.className = 'err';
        closeBtn.style.display = '';
        btn.disabled = false;
      } else {
        appendLog(msg);
      }
    };

    es.onerror = () => {
      es.close();
      // Connection dropped — app likely restarting
      appendLog('--- connection lost, app restarting ---');
      status.textContent = '✓ Deployed — waiting for app…';
      status.className = 'ok';
      pollHealth();
    };
  }

  function pollHealth() {
    let attempts = 0;
    const max = 60;
    const iv = setInterval(async () => {
      attempts++;
      try {
        const r = await fetch('/health', { cache: 'no-store' });
        if (r.ok) {
          clearInterval(iv);
          appendLog('--- app is back online ---');
          status.textContent = '✓ Deployed and running';
          status.className = 'ok';
          closeBtn.style.display = '';
          btn.disabled = false;
        }
      } catch (_) { /* still restarting */ }
      if (attempts >= max) {
        clearInterval(iv);
        status.textContent = 'App did not come back — check logs';
        status.className = 'err';
        closeBtn.style.display = '';
        btn.disabled = false;
      }
    }, 2000);
  }

  btn.addEventListener('click', startDeploy);

  document.getElementById('deploy-modal-close').addEventListener('click', () => {
    overlay.classList.remove('open');
  });
  closeBtn.addEventListener('click', () => {
    overlay.classList.remove('open');
  });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.classList.remove('open');
  });
})();
</script>
"""


