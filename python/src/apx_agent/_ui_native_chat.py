"""Shared native invocation client for the two existing chat renderers."""

NATIVE_CHAT_SCRIPT = r"""
<script>
function apxNativeChat({form, input, send, container, bubble, render}) {
  const key = '_apx_native_chat:' + location.pathname;
  let saved = JSON.parse(sessionStorage.getItem(key) || 'null');
  let session = saved ? saved.session : crypto.randomUUID();
  let busy = false;
  let restoring = false;
  let answer, status, eventLog, controls;
  const persist = () => sessionStorage.setItem(key, JSON.stringify(saved));
  function prepare() {
    document.getElementById('landing')?.remove();
    if (!restoring) bubble('user', saved.request.input.messages[0].content);
    status = document.createElement('div');
    status.className = 'native-status';
    status.setAttribute('role', 'status');
    container.appendChild(status);
    eventLog = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'Run events';
    eventLog.appendChild(summary);
    container.appendChild(eventLog);
    if (!restoring) saved.events.forEach(showEvent);
    controls = document.createElement('div');
    for (const [label, post] of [['Reconnect', false], ['Retry same request', true]]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.disabled = post && restoring;
      button.onclick = () => follow(post);
      controls.appendChild(button);
    }
    container.appendChild(controls);
    answer = null;
  }
  function showEvent(event) {
    const row = document.createElement('pre');
    row.className = 'native-event';
    row.textContent = JSON.stringify(event, null, 2);
    eventLog.appendChild(row);
  }
  async function checked(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response;
  }
  async function events(path) {
    const response = await checked(path + '/events?after=' + saved.cursor);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (true) {
        const {done, value} = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, {stream: true});
        buffer = buffer.replace(/\r\n/g, '\n');
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const frame = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          const lines = frame.split('\n');
          const id = Number(lines.find(line => line.startsWith('id:'))?.slice(3));
          const data = lines.filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
          if (!Number.isSafeInteger(id) || id <= saved.cursor || !data) continue;
          const event = JSON.parse(data);
          saved.events.push(event);
          saved.cursor = id;
          persist();
          showEvent(event);
        }
        if (done) break;
      }
    } finally {
      reader.releaseLock();
    }
  }
  async function follow(post) {
    if (busy || (post && restoring)) return;
    busy = true;
    send.disabled = true;
    controls.hidden = true;
    status.textContent = 'Running';
    const path = '/api/invocations/' + saved.request.id;
    try {
      if (post) await checked('/api/invocations', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(saved.request),
      });
      else await checked(path); // Reconnect reads existing state; it never submits.
      // Check access before displaying cached content after a reload/account switch.
      if (restoring) {
        container.insertBefore(bubble('user', saved.request.input.messages[0].content), status);
        saved.events.forEach(showEvent);
        restoring = false;
        controls.querySelectorAll('button').forEach(button => button.disabled = false);
      }
      await events(path);
      const state = await (await checked(path)).json();
      saved.terminal = state.status === 'completed' || state.status === 'failed';
      if (state.status === 'failed') {
        status.textContent = 'Failed: ' + (typeof state.error === 'string' ? state.error : 'Invocation failed');
      } else if (state.status === 'completed' && state.output?.status === 'interrupted') {
        status.textContent = 'Interrupted: approval required' + (state.output.approval_required?.tool_name ? ' for ' + state.output.approval_required.tool_name : '');
      } else if (state.status === 'completed' && state.output?.status === 'completed') {
        const messages = state.output.messages || [];
        const text = messages.filter(m => m.type === 'ai').map(m =>
          typeof m.content === 'string' ? m.content : (m.content || []).filter(p => p.type === 'text').map(p => p.text).join('')
        ).filter(Boolean).join('\n');
        if (!answer) answer = bubble('assistant', '');
        render(answer, text || '(empty response)');
        status.textContent = 'Completed';
      } else {
        status.textContent = 'Result unavailable. Reconnect to check this run.';
        controls.hidden = false;
      }
      persist();
    } catch (error) {
      status.textContent = 'Connection uncertain (' + error.message + '). Reconnect to check this run'
        + (restoring ? '.' : ', or retry the same request.');
      controls.hidden = false;
    } finally {
      busy = false;
      send.disabled = !saved.terminal;
    }
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    event.stopImmediatePropagation();
    const text = input.value.trim();
    if (!text || busy || (saved && !saved.terminal)) return;
    saved = {session, request: {id: crypto.randomUUID(), session_id: session,
      input: {messages: [{role: 'user', content: text}]}, background: true, stream: true},
      cursor: 0, events: [], terminal: false};
    restoring = false;
    persist();
    input.value = '';
    prepare();
    follow(true);
  }, true);
  if (saved) { restoring = true; prepare(); follow(false); }
}
</script>
"""


def native_chat_script(*, root: bool) -> str:
    """Bind to existing page elements and safe message renderers."""
    bindings = (
        "form, input, send: sendBtn, container: log, bubble, render: renderMarkdown"
        if root else
        "form, input: inputEl, send: sendBtn, container: chat, bubble: addMsg, render: renderAssistantInto"
    )
    return NATIVE_CHAT_SCRIPT + "<script>apxNativeChat({" + bindings + "});</script>"
