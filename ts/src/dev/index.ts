/**
 * Dev UI plugin for Databricks AppKit.
 *
 * Adds development-time routes for testing and inspecting the agent:
 * - /_apx/agent — chat UI for interactive testing
 * - /_apx/tools — tool inspector with live invocation forms
 * - /_apx/probe?url=<url> — outbound connectivity tester
 *
 * Usage:
 *   import { devUI } from 'appkit-agent';
 *
 *   createApp({
 *     plugins: [
 *       agent({ model: '...', tools: [...] }),
 *       devUI(),
 *     ],
 *   });
 */

import type { IAppRouter } from '@databricks/appkit';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface DevUIConfig {
  /** Base path for dev UI routes. Defaults to '/_apx'. */
  basePath?: string;
  /** Disable in production. Defaults to true (enabled only when NODE_ENV !== 'production'). */
  productionGuard?: boolean;
}

// ---------------------------------------------------------------------------
// Dev UI plugin factory
// ---------------------------------------------------------------------------

export function devUI(config: DevUIConfig = {}) {
  const basePath = config.basePath ?? '/_apx';
  const guardProduction = config.productionGuard ?? true;

  return {
    name: 'devUI',
    displayName: 'Agent Dev UI',
    description: 'Development chat UI and tool inspector',

    injectRoutes(router: IAppRouter) {
      if (guardProduction && process.env.NODE_ENV === 'production') {
        return; // Don't mount dev routes in production
      }

      // Tool inspector — lists all registered tools with schemas
      router.get(`${basePath}/tools`, (req, res) => {
        // TODO: Pull tool schemas from agent plugin exports
        res.json({
          message: 'Tool inspector — coming soon',
          hint: 'Tools are available at /api/tools/:name',
        });
      });

      // Chat UI — serves a minimal HTML page for testing the agent
      router.get(`${basePath}/agent`, (_req, res) => {
        res.type('html').send(chatPageHtml(basePath));
      });

      // Outbound connectivity probe
      router.get(`${basePath}/probe`, async (req, res) => {
        const targetUrl = req.query.url as string;
        if (!targetUrl) {
          res.status(400).json({ error: 'url query parameter required' });
          return;
        }
        try {
          const start = Date.now();
          const response = await fetch(targetUrl);
          const elapsed = Date.now() - start;
          res.json({
            url: targetUrl,
            status: response.status,
            ok: response.ok,
            elapsed_ms: elapsed,
          });
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          res.json({ url: targetUrl, error: message });
        }
      });
    },
  };
}

function chatPageHtml(basePath: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Agent Dev UI</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0; height: 100vh; display: flex; flex-direction: column; }
    header { padding: 1rem; background: #16213e; border-bottom: 1px solid #333; }
    header h1 { font-size: 1.1rem; font-weight: 600; }
    #messages { flex: 1; overflow-y: auto; padding: 1rem; }
    .msg { margin-bottom: 0.75rem; padding: 0.75rem; border-radius: 8px; max-width: 80%; white-space: pre-wrap; }
    .msg.user { background: #0f3460; margin-left: auto; }
    .msg.assistant { background: #1a1a2e; border: 1px solid #333; }
    #input-bar { display: flex; gap: 0.5rem; padding: 1rem; background: #16213e; border-top: 1px solid #333; }
    #input-bar input { flex: 1; padding: 0.75rem; border-radius: 6px; border: 1px solid #444; background: #1a1a2e; color: #e0e0e0; font-size: 0.9rem; }
    #input-bar button { padding: 0.75rem 1.5rem; border-radius: 6px; border: none; background: #e94560; color: white; font-weight: 600; cursor: pointer; }
    nav { padding: 0.5rem 1rem; background: #16213e; font-size: 0.8rem; }
    nav a { color: #e94560; margin-right: 1rem; }
  </style>
</head>
<body>
  <header><h1>Agent Dev UI</h1></header>
  <nav>
    <a href="${basePath}/agent">Chat</a>
    <a href="${basePath}/tools">Tools</a>
    <a href="/.well-known/agent.json">Agent Card</a>
  </nav>
  <div id="messages"></div>
  <div id="input-bar">
    <input id="input" type="text" placeholder="Ask the agent..." autofocus />
    <button onclick="send()">Send</button>
  </div>
  <script>
    const msgs = document.getElementById('messages');
    const input = document.getElementById('input');
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });

    async function send() {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      addMsg('user', text);
      try {
        const res = await fetch('/invocations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ input: [{ role: 'user', content: text }] }),
        });
        const data = await res.json();
        const reply = data?.output?.[0]?.content?.[0]?.text ?? JSON.stringify(data);
        addMsg('assistant', reply);
      } catch (err) {
        addMsg('assistant', 'Error: ' + err.message);
      }
    }

    function addMsg(role, text) {
      const div = document.createElement('div');
      div.className = 'msg ' + role;
      div.textContent = text;
      msgs.appendChild(div);
      msgs.scrollTop = msgs.scrollHeight;
    }
  </script>
</body>
</html>`;
}
