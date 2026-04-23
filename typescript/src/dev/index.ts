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
import { getTraces, getTrace } from '../trace.js';
import type { Trace, TraceSpan } from '../trace.js';

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

        // Validate URL scheme and block private/internal addresses
        let parsed: URL;
        try {
          parsed = new URL(targetUrl);
        } catch {
          res.status(400).json({ error: 'Invalid URL' });
          return;
        }
        if (!['http:', 'https:'].includes(parsed.protocol)) {
          res.status(400).json({ error: 'Only http/https URLs allowed' });
          return;
        }
        const host = parsed.hostname.toLowerCase();
        if (
          host === 'localhost' ||
          host.startsWith('127.') ||
          host.startsWith('10.') ||
          host.startsWith('192.168.') ||
          host === '169.254.169.254' ||
          host.startsWith('0.') ||
          host === '[::1]' ||
          /^172\.(1[6-9]|2\d|3[01])\./.test(host)
        ) {
          res.status(403).json({ error: 'Private/internal addresses not allowed' });
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

      // Trace list — recent traces overview
      router.get(`${basePath}/traces`, (_req: Request, res: Response) => {
        const traces = getTraces();
        res.setHeader('Content-Type', 'text/html');
        res.send(tracesListHtml(traces, basePath));
      });

      // Trace detail — single trace conversation view
      router.get(`${basePath}/traces/:traceId`, (req: Request, res: Response) => {
        const trace = getTrace(req.params.traceId as string);
        if (!trace) { res.status(404).send('Trace not found'); return; }
        res.setHeader('Content-Type', 'text/html');
        res.send(traceDetailHtml(trace, basePath));
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
    <a href="${basePath}/traces">Traces</a>
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

// ---------------------------------------------------------------------------
// Trace list HTML
// ---------------------------------------------------------------------------

function truncateStr(value: unknown, maxLen = 120): string {
  const s = typeof value === 'string' ? value : JSON.stringify(value);
  if (!s) return '';
  return s.length > maxLen ? s.slice(0, maxLen) + '...' : s;
}

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function statusBadge(status?: string): string {
  const colors: Record<string, string> = {
    in_progress: '#f0ad4e',
    completed: '#5cb85c',
    error: '#d9534f',
  };
  const color = colors[status ?? ''] ?? '#888';
  return `<span style="display:inline-block;padding:2px 8px;border-radius:4px;background:${color};color:#fff;font-size:0.75rem;font-weight:600;">${escapeHtml(status ?? 'unknown')}</span>`;
}

function tracesListHtml(traces: Trace[], basePath: string): string {
  const total = traces.length;
  const inProgress = traces.filter((t) => t.status === 'in_progress').length;
  const completed = traces.filter((t) => t.status === 'completed').length;
  const errored = traces.filter((t) => t.status === 'error').length;

  const rows = traces
    .map((t) => {
      const firstInput = t.spans.find((s) => s.type === 'request');
      const inputPreview = firstInput ? truncateStr(firstInput.input, 80) : '';
      const duration = t.duration_ms != null ? `${t.duration_ms}ms` : 'running';
      return `<tr onclick="location.href='${basePath}/traces/${t.id}'" style="cursor:pointer;">
        <td style="padding:8px 12px;border-bottom:1px solid #333;font-family:monospace;font-size:0.8rem;">${escapeHtml(t.id)}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;">${escapeHtml(t.agentName)}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;">${statusBadge(t.status)}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;text-align:center;">${t.spans.length}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;text-align:right;font-family:monospace;">${duration}</td>
        <td style="padding:8px 12px;border-bottom:1px solid #333;font-size:0.85rem;color:#aaa;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(inputPreview)}</td>
      </tr>`;
    })
    .join('\n');

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="10">
  <title>Agent Traces</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0; min-height: 100vh; }
    header { padding: 1rem; background: #16213e; border-bottom: 1px solid #333; }
    header h1 { font-size: 1.1rem; font-weight: 600; }
    nav { padding: 0.5rem 1rem; background: #16213e; font-size: 0.8rem; }
    nav a { color: #e94560; margin-right: 1rem; text-decoration: none; }
    nav a:hover { text-decoration: underline; }
    .summary { padding: 1rem; display: flex; gap: 1.5rem; font-size: 0.85rem; color: #aaa; }
    .summary span { font-weight: 600; color: #e0e0e0; }
    table { width: 100%; border-collapse: collapse; }
    th { text-align: left; padding: 8px 12px; border-bottom: 2px solid #444; font-size: 0.8rem; color: #aaa; text-transform: uppercase; letter-spacing: 0.05em; }
    tr:hover { background: #16213e; }
  </style>
</head>
<body>
  <header><h1>Agent Traces</h1></header>
  <nav>
    <a href="${basePath}/agent">Chat</a>
    <a href="${basePath}/tools">Tools</a>
    <a href="${basePath}/traces">Traces</a>
    <a href="/.well-known/agent.json" target="_blank">Agent Card</a>
  </nav>
  <div class="summary">
    <div>Total: <span>${total}</span></div>
    <div>In Progress: <span>${inProgress}</span></div>
    <div>Completed: <span>${completed}</span></div>
    <div>Errors: <span>${errored}</span></div>
  </div>
  <table>
    <thead>
      <tr>
        <th>Trace ID</th>
        <th>Agent</th>
        <th>Status</th>
        <th>Spans</th>
        <th>Duration</th>
        <th>Input</th>
      </tr>
    </thead>
    <tbody>
      ${rows || '<tr><td colspan="6" style="padding:2rem;text-align:center;color:#666;">No traces yet</td></tr>'}
    </tbody>
  </table>
</body>
</html>`;
}

// ---------------------------------------------------------------------------
// Trace detail HTML
// ---------------------------------------------------------------------------

function spanBubble(span: TraceSpan): string {
  const colors: Record<TraceSpan['type'], { bg: string; label: string; accent: string }> = {
    request:    { bg: '#2a2a3e', label: 'Incoming Request',  accent: '#888' },
    llm:        { bg: '#0a2a3e', label: 'LLM',              accent: '#00bcd4' },
    tool:       { bg: '#2a2500', label: 'Tool',              accent: '#ffb300' },
    agent_call: { bg: '#1a0a2e', label: 'Agent',            accent: '#ab47bc' },
    response:   { bg: '#0a2a0a', label: 'Response',         accent: '#4caf50' },
    error:      { bg: '#2a0a0a', label: 'Error',            accent: '#f44336' },
  };

  const c = colors[span.type] ?? colors.request;
  const duration = span.duration_ms != null ? `<span style="float:right;font-size:0.75rem;color:#888;">${span.duration_ms}ms</span>` : '';

  let title = c.label;
  if (span.type === 'llm' && span.metadata?.model) {
    title = `LLM → ${escapeHtml(String(span.metadata.model))}`;
  } else if (span.type === 'tool') {
    title = `Tool → ${escapeHtml(span.name)}`;
  } else if (span.type === 'agent_call') {
    title = `Agent → ${escapeHtml(span.name)}`;
  }

  let body = '';
  if (span.input != null) {
    const label = span.type === 'tool' ? 'Params' : 'Input';
    body += `<div style="margin-top:0.5rem;"><strong style="font-size:0.75rem;color:${c.accent};">${label}:</strong><pre style="margin:4px 0 0;white-space:pre-wrap;word-break:break-all;font-size:0.8rem;color:#ccc;font-family:monospace;">${escapeHtml(truncateStr(span.input, 500))}</pre></div>`;
  }
  if (span.output != null) {
    const label = span.type === 'tool' ? 'Result' : 'Output';
    body += `<div style="margin-top:0.5rem;"><strong style="font-size:0.75rem;color:${c.accent};">${label}:</strong><pre style="margin:4px 0 0;white-space:pre-wrap;word-break:break-all;font-size:0.8rem;color:#ccc;font-family:monospace;">${escapeHtml(truncateStr(span.output, 500))}</pre></div>`;
  }
  if (span.type === 'error' && span.metadata?.error) {
    body += `<div style="margin-top:0.5rem;"><strong style="font-size:0.75rem;color:${c.accent};">Details:</strong><pre style="margin:4px 0 0;white-space:pre-wrap;word-break:break-all;font-size:0.8rem;color:#f88;font-family:monospace;">${escapeHtml(truncateStr(span.metadata.error, 500))}</pre></div>`;
  }

  return `<div style="margin-bottom:0.75rem;padding:0.75rem 1rem;border-radius:8px;background:${c.bg};border-left:3px solid ${c.accent};">
    <div style="font-size:0.85rem;font-weight:600;color:${c.accent};">${title}${duration}</div>
    ${body}
  </div>`;
}

function traceDetailHtml(trace: Trace, basePath: string): string {
  const duration = trace.duration_ms != null ? `${trace.duration_ms}ms` : 'running';
  const spans = trace.spans.map(spanBubble).join('\n');

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Trace: ${escapeHtml(trace.agentName)} — ${escapeHtml(trace.id)}</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0; min-height: 100vh; }
    header { padding: 1rem; background: #16213e; border-bottom: 1px solid #333; }
    header h1 { font-size: 1.1rem; font-weight: 600; }
    nav { padding: 0.5rem 1rem; background: #16213e; font-size: 0.8rem; }
    nav a { color: #e94560; margin-right: 1rem; text-decoration: none; }
    nav a:hover { text-decoration: underline; }
    .trace-meta { padding: 1rem; display: flex; gap: 1.5rem; align-items: center; font-size: 0.85rem; color: #aaa; border-bottom: 1px solid #333; }
    .trace-meta .id { font-family: monospace; font-size: 0.8rem; color: #e0e0e0; }
    .spans { padding: 1rem; max-width: 900px; }
  </style>
</head>
<body>
  <header><h1>${escapeHtml(trace.agentName)}</h1></header>
  <nav>
    <a href="${basePath}/traces">&larr; Back to Traces</a>
    <a href="${basePath}/agent">Chat</a>
    <a href="${basePath}/tools">Tools</a>
  </nav>
  <div class="trace-meta">
    <div class="id">${escapeHtml(trace.id)}</div>
    <div>${statusBadge(trace.status)}</div>
    <div>Duration: <strong>${duration}</strong></div>
    <div>Spans: <strong>${trace.spans.length}</strong></div>
  </div>
  <div class="spans">
    ${spans || '<div style="padding:2rem;text-align:center;color:#666;">No spans recorded</div>'}
  </div>
</body>
</html>`;
}
