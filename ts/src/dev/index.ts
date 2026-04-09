/**
 * Dev UI plugin for Databricks AppKit.
 *
 * Adds development-time routes for testing and inspecting the agent:
 * - /_apx/agent — chat UI with streaming support
 * - /_apx/tools — tool inspector with real schemas
 * - /_apx/probe?url=<url> — outbound connectivity tester
 */

import type { Request, Response } from 'express';
import type { AgentExports } from '../agent/index.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface DevUIConfig {
  /** Base path for dev UI routes. Defaults to '/_apx'. */
  basePath?: string;
  /** Disable in production. Defaults to true. */
  productionGuard?: boolean;
}

// ---------------------------------------------------------------------------
// Plugin factory
// ---------------------------------------------------------------------------

export function createDevPlugin(config: DevUIConfig, agentExports: () => AgentExports | null) {
  const basePath = config.basePath ?? '/_apx';
  const guardProduction = config.productionGuard ?? true;

  return {
    name: 'devUI' as const,
    displayName: 'Agent Dev UI',
    description: 'Development chat UI and tool inspector',

    injectRoutes(router: { get: Function }) {
      if (guardProduction && process.env.NODE_ENV === 'production') {
        return;
      }

      // Tool inspector — returns real schemas from agent plugin
      router.get(`${basePath}/tools`, (_req: Request, res: Response) => {
        const exports = agentExports();
        if (!exports) {
          res.json({ tools: [], message: 'Agent plugin not available' });
          return;
        }
        const tools = exports.getTools().map((t) => ({
          name: t.name,
          description: t.description,
        }));
        const schemas = exports.getToolSchemas();
        res.json({ tools, schemas });
      });

      // Chat UI with SSE streaming support
      router.get(`${basePath}/agent`, (_req: Request, res: Response) => {
        res.type('html').send(chatPageHtml(basePath));
      });

      // Outbound connectivity probe
      router.get(`${basePath}/probe`, async (req: Request, res: Response) => {
        const targetUrl = req.query.url as string;
        if (!targetUrl) {
          res.status(400).json({ error: 'url query parameter required' });
          return;
        }
        try {
          const start = Date.now();
          const response = await fetch(targetUrl);
          res.json({
            url: targetUrl,
            status: response.status,
            ok: response.ok,
            elapsed_ms: Date.now() - start,
          });
        } catch (err) {
          res.json({ url: targetUrl, error: err instanceof Error ? err.message : String(err) });
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
    nav a { color: #e94560; margin-right: 1rem; text-decoration: none; }
    nav a:hover { text-decoration: underline; }
    .streaming { opacity: 0.7; }
  </style>
</head>
<body>
  <header><h1>Agent Dev UI</h1></header>
  <nav>
    <a href="${basePath}/agent">Chat</a>
    <a href="${basePath}/tools">Tools</a>
    <a href="/.well-known/agent.json" target="_blank">Agent Card</a>
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

      // Try streaming first
      const assistantDiv = addMsg('assistant', '', true);
      try {
        const res = await fetch('/responses', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ input: [{ role: 'user', content: text }], stream: true }),
        });

        if (res.headers.get('content-type')?.includes('text/event-stream')) {
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let fullText = '';
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            const chunk = decoder.decode(value);
            for (const line of chunk.split('\\n')) {
              if (line.startsWith('data: ')) {
                try {
                  const data = JSON.parse(line.slice(6));
                  if (data.text) { fullText += data.text; assistantDiv.textContent = fullText; }
                  if (data.output) { assistantDiv.textContent = data.output?.content?.[0]?.text || fullText; }
                } catch {}
              }
            }
          }
          assistantDiv.classList.remove('streaming');
        } else {
          const data = await res.json();
          const reply = data?.output_text ?? data?.output?.[0]?.content?.[0]?.text ?? JSON.stringify(data);
          assistantDiv.textContent = reply;
          assistantDiv.classList.remove('streaming');
        }
      } catch (err) {
        assistantDiv.textContent = 'Error: ' + err.message;
        assistantDiv.classList.remove('streaming');
      }
      msgs.scrollTop = msgs.scrollHeight;
    }

    function addMsg(role, text, streaming = false) {
      const div = document.createElement('div');
      div.className = 'msg ' + role + (streaming ? ' streaming' : '');
      div.textContent = text || (streaming ? 'Thinking...' : '');
      msgs.appendChild(div);
      msgs.scrollTop = msgs.scrollHeight;
      return div;
    }
  </script>
</body>
</html>`;
}
