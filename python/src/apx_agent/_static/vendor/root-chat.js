// End-user chat at /. Served by the root chat router, not the dev UI (#1052).
const log = document.getElementById('log');
  const input = document.getElementById('in');
  const sendBtn = document.getElementById('send');
  const form = document.getElementById('f');
  const history = [];  // [{role, content}] sent as the /invocations `input`

  const hasMd = typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined';
  function renderMarkdown(el, text) {
    if (hasMd) el.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
    else el.textContent = text || '';
  }

  function bubble(role, text) {
    const el = document.createElement('div');
    el.className = 'msg ' + role;
    if (role === 'assistant') renderMarkdown(el, text);
    else el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function errLine(text) {
    const el = document.createElement('div');
    el.className = 'err';
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
  }

  async function send(text) {
    if (window.__APX_NATIVE__) return;
    history.push({ role: 'user', content: text });
    bubble('user', text);
    sendBtn.disabled = true;
    const pending = bubble('assistant', '');
    pending.classList.add('dots');
    try {
      const resp = await fetch('/invocations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: history }),
      });
      if (!resp.ok) throw new Error(resp.status + ' ' + (await resp.text()).slice(0, 300));
      const data = await resp.json();
      // ResponsesAgent output: items of {type}. Collect assistant text from
      // `message` items; ignore function_call / function_call_output (tools).
      const out = Array.isArray(data.output) ? data.output : [];
      let reply = '';
      for (const item of out) {
        if (item.type === 'message' && Array.isArray(item.content)) {
          for (const p of item.content) {
            if (p.type === 'output_text' && p.text) reply += p.text;
          }
        }
      }
      pending.classList.remove('dots');
      renderMarkdown(pending, reply || '(empty response)');
      history.push({ role: 'assistant', content: reply });
    } catch (e) {
      pending.remove();
      errLine('Error: ' + e.message);
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.style.height = 'auto';
    send(text);
  });

  // Enter sends, Shift+Enter newlines; textarea grows with content.
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = input.scrollHeight + 'px';
  });
