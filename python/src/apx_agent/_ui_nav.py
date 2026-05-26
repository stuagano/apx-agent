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


# Single source of truth for dev-UI navigation. Each entry is
# ``(slug, label)``; the link target is ``/_apx/<slug>``. Every page renders
# its nav from this list (via _apx_nav_links) so the bar can't drift out of
# sync with the routes that actually exist. Keep aligned with the real GET
# routes in _dev.py — test_dev_ui_routes.py enforces it.
APX_NAV_PAGES: list[tuple[str, str]] = [
    ("agent", "Chat"),
    ("edit", "Edit"),
    ("eval", "Eval"),
    ("setup", "Setup"),
    ("probe", "Probe"),
    ("topology", "Topology"),
]


def _apx_nav_links(active: str) -> str:
    """Return the ``<a>`` tags for the canonical nav, marking ``active``.

    Pages embed this inside their own ``<nav>`` so they share one link list
    while keeping their existing header chrome.
    """
    active_cls = 'class="active"'
    return "".join(
        f'<a href="/_apx/{slug}" {active_cls if slug == active else ""}>{label}</a>'
        for slug, label in APX_NAV_PAGES
    )


def _apx_nav_html(active: str) -> str:
    links = _apx_nav_links(active)
    # Hide the per-page nav when this page is loaded inside the unified
    # shell at /_apx/agent — the shell renders its own tab bar and a second
    # nav row would be redundant. Also hide any page-level <header> element
    # (the chat page has its own, separate from #apx-header). Standalone
    # visits to /_apx/edit, /_apx/topology, etc. still see their nav.
    return f"""<div id="apx-header"><div id="apx-nav">
  <span class="badge">APX dev</span>
  <nav>{links}</nav>
</div></div>
<script>if (window.self !== window.top) {{
  var h = document.getElementById("apx-header"); if (h) h.style.display = "none";
  document.querySelectorAll("body > header").forEach(function (el) {{ el.style.display = "none"; }});
}}</script>"""


def _deploy_overlay_html() -> str:
    """Shared deploy modal + SSE log viewer injected into every /_apx/ page.

    Also carries the iframe-suppression script: when the page is loaded
    inside the unified shell at /_apx/agent, its own page-level <header>
    + shared #apx-header are hidden so the shell's tab strip is the only
    nav row visible. Standalone visits keep the per-page nav intact.
    """
    return """
<script>
  if (window.self !== window.top) {
    document.documentElement.classList.add("apx-embedded");
    document.addEventListener("DOMContentLoaded", function () {
      var apxHdr = document.getElementById("apx-header");
      if (apxHdr) apxHdr.style.display = "none";
      document.querySelectorAll("body > header").forEach(function (el) {
        el.style.display = "none";
      });
    });
  }
</script>
""" + """
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


def _topology_minimap_html() -> str:
    """A floating topology minimap for embedding on a dev-UI page.

    Three states, toggled by classes on ``#apx-mm``:
      * default — a thumbnail card pinned bottom-right showing the whole graph
        (a transparent catcher over the iframe expands it on click);
      * ``.expanded`` — a large interactive panel (catcher removed, so pan/zoom
        and node clicks work);
      * ``.min`` — collapsed to a small "⬡ Topology" pill, out of the way.

    Reuses the existing ``/_apx/topology`` react-flow app via an iframe rather
    than re-rendering the graph, so it always matches the dedicated view.
    """
    return """
<style>
  #apx-mm { position: fixed; right: 16px; bottom: 16px; z-index: 1200;
            width: 320px; height: 220px; background: #0d0d0d;
            border: 1px solid #2a2a2a; border-radius: 10px; overflow: hidden;
            box-shadow: 0 8px 30px rgba(0,0,0,.55); display: flex;
            flex-direction: column; transition: width .22s ease, height .22s ease; }
  #apx-mm.expanded { width: min(880px, 78vw); height: min(620px, 78vh); }
  #apx-mm-bar { height: 28px; flex-shrink: 0; display: flex; align-items: center;
                gap: 6px; padding: 0 8px; background: #141414;
                border-bottom: 1px solid #1e1e1e; cursor: default; user-select: none; }
  #apx-mm-bar .t { font-size: 11px; font-weight: 600; color: #9aa; letter-spacing: .4px;
                   text-transform: uppercase; }
  #apx-mm-bar .sp { margin-left: auto; }
  #apx-mm-bar button { background: none; border: none; color: #667; cursor: pointer;
                       font-size: 13px; line-height: 1; padding: 3px 5px; border-radius: 4px; }
  #apx-mm-bar button:hover { color: #ccc; background: #222; }
  #apx-mm-body { flex: 1; position: relative; }
  #apx-mm-frame { width: 100%; height: 100%; border: 0; background: #0d0d0d; }
  /* Transparent catcher: lets a thumbnail click expand the widget. Removed in
     expanded state so the graph itself receives pan/zoom/click events. */
  #apx-mm-catch { position: absolute; inset: 0; cursor: zoom-in; background: transparent; }
  #apx-mm.expanded #apx-mm-catch { display: none; }
  #apx-mm.min { width: auto; height: auto; }
  #apx-mm.min #apx-mm-bar, #apx-mm.min #apx-mm-body { display: none; }
  #apx-mm-pill { display: none; align-items: center; gap: 6px; cursor: pointer;
                 padding: 7px 12px; font-size: 12px; font-weight: 600; color: #9bf;
                 background: #0d1f38; border: none; }
  #apx-mm.min #apx-mm-pill { display: flex; }
</style>
<div id="apx-mm" aria-label="Agent topology minimap">
  <div id="apx-mm-bar">
    <span class="t">⬡ Topology</span>
    <span class="sp"></span>
    <button id="apx-mm-expand" title="Expand / collapse">⤢</button>
    <button id="apx-mm-min" title="Minimize">–</button>
  </div>
  <div id="apx-mm-body">
    <iframe id="apx-mm-frame" title="Agent topology" src="/_apx/topology?embed=1"></iframe>
    <div id="apx-mm-catch" title="Click to expand"></div>
  </div>
  <button id="apx-mm-pill" title="Show topology">⬡ Topology</button>
</div>
<script>
(function () {
  var mm = document.getElementById('apx-mm');
  if (!mm) return;
  var expandBtn = document.getElementById('apx-mm-expand');
  var minBtn = document.getElementById('apx-mm-min');
  var catch_ = document.getElementById('apx-mm-catch');
  var pill = document.getElementById('apx-mm-pill');
  function setExpanded(on) {
    mm.classList.toggle('expanded', on);
    expandBtn.textContent = on ? '⤡' : '⤢';
  }
  catch_.addEventListener('click', function () { setExpanded(true); });
  expandBtn.addEventListener('click', function () { setExpanded(!mm.classList.contains('expanded')); });
  minBtn.addEventListener('click', function () { mm.classList.add('min'); mm.classList.remove('expanded'); expandBtn.textContent = '⤢'; });
  pill.addEventListener('click', function () { mm.classList.remove('min'); });
})();
</script>
"""


