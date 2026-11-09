// ── History tab ──
// Extracted verbatim from the legacy chat page's inline script (#1052). Loaded as a
// classic, parser-blocking script at the same position, so it keeps sharing the page's
// globals (devThreadId, _THREAD_KEY, chat, history, esc, promoteToEval,
// switchConversation) and runs in the original order. Native hosts inline this exact
// file instead of fetching it — see _ui_chat.py.
let _convHistoryTimer = null;
function loadConversationHistory() {
  if (_convHistoryTimer) clearTimeout(_convHistoryTimer);
  _convHistoryTimer = setTimeout(_doLoadConversationHistory, 120);
}
function _doLoadConversationHistory() {
  _convHistoryTimer = null;
  fetch('/_apx/conversations').then(r => r.json()).then(convs => {
    const list = document.getElementById('conv-list');
    if (!convs.length) {
      list.innerHTML = '<div class="empty-state">No conversations yet — send a message</div>';
      return;
    }
    list.innerHTML = convs.map(c => {
      const label = c.title || ('Conv ' + c.id.slice(-8));
      const ts = c.updated_at ? new Date(c.updated_at).toLocaleDateString() : '';
      const active = c.id === devThreadId ? ' active' : '';
      return `<div class="conv-item${active}" data-id="${c.id}" onclick="switchConversation('${c.id}')">
        <div style="display:flex;justify-content:space-between;align-items:flex-start">
          <div class="conv-title" style="flex:1">${esc(label)}</div>
          <button onclick="event.stopPropagation();promoteToEval('${c.id}',${JSON.stringify(label)})"
            title="Promote to Eval"
            style="background:transparent;border:1px solid var(--apx-border);border-radius:3px;color:var(--apx-muted);font-size:9px;padding:1px 5px;cursor:pointer;flex-shrink:0;margin-left:4px">→ Eval</button>
        </div>
        ${ts ? `<div class="conv-ts">${ts}</div>` : ''}
      </div>`;
    }).join('');
  }).catch(() => {});
}

function newConversation() {
  devThreadId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36);
  sessionStorage.setItem(_THREAD_KEY, devThreadId);
  chat.innerHTML = '';
  history.length = 0;  // reset request payload so a new conversation starts clean
  loadConversationHistory();
}
