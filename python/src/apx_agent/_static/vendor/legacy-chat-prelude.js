// Legacy chat prelude (tabs, examples, MCP). Classic script shared with
// legacy-chat-history.js and legacy-chat-app.js (#1052).
function useExample(btn) {
  const inp = document.getElementById('input');
  inp.value = btn.dataset.q;
  form.requestSubmit();
}
// Table selected from the workspace drawer (parent frame postMessage)
window.addEventListener('message', (e) => {
  if (e.data?.type !== 'apx:table-selected') return;
  const fqn = e.data.fqn || '';
  const table = fqn.split('.').pop();
  const questions = [
    `What columns does ${fqn} have?`,
    `Show me a sample of data from ${table}`,
    `How many rows are in ${table}?`,
    `What are the most recent records in ${table}?`,
  ];
  const chips = questions.map(q =>
    `<button type="button" class="starter-chip" onclick="useExample(this)" data-q="${q}">${q}</button>`
  ).join('');
  const chatEl = document.getElementById('chat');
  chatEl.innerHTML = `
    <div style="padding:32px 24px 12px">
      <div style="font-size:11px;color:var(--apx-muted);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px">Table selected</div>
      <div style="font-family:ui-monospace,monospace;font-size:13px;color:var(--apx-text);margin-bottom:16px">${fqn}</div>
      <div class="starter-chips">${chips}</div>
    </div>`;
  document.getElementById('input').placeholder = 'Ask about ${table}…';
  document.getElementById('input').focus();
});
const chat = document.getElementById('chat');
const form = document.getElementById('form');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const eventsList = document.getElementById('events-list');
const toolsEmbed = document.getElementById('tools-embed');
let toolsInspectorLoaded = false;
function ensureToolsInspector() {
  if (toolsInspectorLoaded || !toolsEmbed) return;
  const frame = document.createElement('iframe');
  frame.id = 'tools-inspector-frame';
  frame.title = 'Tool inspector';
  frame.src = '/_apx/tools';
  toolsEmbed.appendChild(frame);
  toolsInspectorLoaded = true;
}
const detailPanel = document.getElementById('detail-panel');
const detailTitle = document.getElementById('detail-title');
const detailBody = document.getElementById('detail-body');
const tooltip = document.getElementById('tooltip');

// ── MCP URL ──
const mcpBar = document.getElementById('mcp-bar');
const mcpUrlEl = document.getElementById('mcp-url');
if (mcpUrlEl) {
  // /mcp = stateless HTTP (Genie Code, AI Playground)
  // /mcp/sse = SSE transport (Claude Desktop, Cursor)
  mcpUrlEl.textContent = `${window.location.origin}/mcp`;
  mcpBar.style.display = 'flex';
}
function copyMcpUrl() {
  navigator.clipboard.writeText(`${window.location.origin}/mcp`).then(() => {
    const ok = document.getElementById('copy-ok');
    ok.style.display = 'inline';
    setTimeout(() => ok.style.display = 'none', 1500);
  });
}
function copyMcpSseUrl() {
  navigator.clipboard.writeText(`${window.location.origin}/mcp/sse`).then(() => {
    const ok = document.getElementById('copy-sse-ok');
    if (ok) { ok.style.display = 'inline'; setTimeout(() => ok.style.display = 'none', 1500); }
  });
}

// ── Tab switching ──
function switchTab(name, btn) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.panel-tabs button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'tools') ensureToolsInspector();
  if (name === 'eval' && (!evalLoaded || !evalRows.length)) loadEvalCases();
  if (name === 'history') loadConversationHistory();
}
