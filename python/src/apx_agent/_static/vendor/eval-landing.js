// Eval landing: run cases against the live agent (#1052).
// Extracted from the eval page's inline script. The page sets
// window.__APX_EVAL_ROWS__ and window.__APX_EVAL_NATIVE__ before this file.
// judge-alignment.js loads next and uses esc().
let rows = window.__APX_EVAL_ROWS__;
const NATIVE = window.__APX_EVAL_NATIVE__;

// A durable host: submit the case in a fresh session (no state leaks between cases),
// wait for the run to finish, and read the answer the native chat shows.
async function runNative(question) {
  const id = crypto.randomUUID();
  const post = await fetch('/api/invocations', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, session_id: crypto.randomUUID(), background: true,
      input: {messages: [{role: 'user', content: question}]}}),
  });
  if (!post.ok) throw new Error(`${post.status} ${await post.text()}`);
  for (;;) {
    const got = await fetch('/api/invocations/' + encodeURIComponent(id));
    if (!got.ok) throw new Error(`${got.status} ${await got.text()}`);
    const state = await got.json();
    if (state.status === 'failed') throw new Error(state.error?.message || 'run failed');
    if (state.status === 'completed') {
      const messages = state.output?.messages || [];
      const halt = messages.filter(m => m.type === 'ai' && m.additional_kwargs?.apx_answer === true);
      return (halt.length ? halt : messages.filter(m => m.type === 'ai')).map(m =>
        typeof m.content === 'string' ? m.content : (m.content || []).filter(p => p.type === 'text').map(p => p.text).join('')
      ).filter(Boolean).join('\n');
    }
    await new Promise(done => setTimeout(done, 1500));
  }
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

function render() {
  const el = document.getElementById('cases');
  if (!rows.length) { el.innerHTML = ''; return; }
  el.innerHTML = rows.map((r, i) => {
    const cls = r.status === 'pass' ? 'pass' : r.status === 'fail' ? 'fail' : '';
    const badge = r.status === 'running'
      ? '<span class="badge badge-running">running…</span>'
      : r.status === 'pass' ? '<span class="badge badge-pass">PASS</span>'
      : r.status === 'fail' ? '<span class="badge badge-fail">FAIL</span>'
      : '';
    const runBtn = r.status !== 'running'
      ? `<button class="run-btn" onclick="runCase(${i})" title="Run" aria-label="Run">&#9654;</button>` : '';
    const criterion = r.expected_judge || r.criterion || '';
    const expected = r.expected || '';
    return `<div class="case ${cls}">
      <div class="case-head">
        <span class="case-n">#${i+1}</span>
        <span class="case-q">${esc(r.question || '')}</span>
        ${badge} ${runBtn}
      </div>
      ${criterion ? `<div class="case-row"><span class="label">Criterion</span><span>${esc(criterion)}</span></div>` : ''}
      ${expected ? `<div class="case-row"><span class="label">Expected</span><span>${esc(expected)}</span></div>` : ''}
      ${r.trace_id ? `<div class="case-row"><span class="label">Source trace</span><a href="/_apx/traces/${encodeURIComponent(r.trace_id)}" target="_blank" style="color:var(--apx-accent);font-size:11px">${r.trace_id.split('/').pop().slice(0,16)}…</a></div>` : ''}
      ${r.response ? `<div class="case-response">${esc(r.response)}</div>` : ''}
      ${r.judge_reason ? `<div class="case-reason">${esc(r.judge_reason)}</div>` : ''}
    </div>`;
  }).join('');
}

async function runCase(i) {
  const r = rows[i];
  r.status = 'running'; r.response = ''; r.judge_verdict = null; r.judge_reason = null;
  render();
  let text = '';
  try {
    if (NATIVE) text = await runNative(r.question);
    else {
    const resp = await fetch('/invocations', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({input: [{role: 'user', content: r.question}], stream: true}),
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const d = JSON.parse(line.slice(6));
          if (d.type === 'response.output_text.delta' && d.delta) { text += d.delta; }
          else if (d.type === 'response.output_item.done') {
            const item = d.item || {};
            if (item.type === 'message' && Array.isArray(item.content)) {
              for (const p of item.content) { if (p.type === 'output_text' && p.text) text += p.text; }
            }
          } else if (d.type === 'response.completed' && !text) {
            const out = d.response && d.response.output;
            if (Array.isArray(out)) for (const it of out) if (it.type === 'message' && Array.isArray(it.content))
              for (const p of it.content) if (p.type === 'output_text' && p.text) text += p.text;
          }
        } catch {}
      }
    }
    }
    r.response = text || '(no response)';
    const criterion = r.expected_judge || r.criterion || '';
    if (criterion) {
      const j = await fetch('/_apx/eval/judge', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({question: r.question, response: text, criterion}),
      });
      const jd = await j.json();
      r.status = jd.ok && jd.pass ? 'pass' : 'fail';
      r.judge_verdict = jd.verdict || 'ERROR';
      r.judge_reason = jd.reason || jd.error || '';
    } else {
      r.status = text.length > 10 ? 'pass' : 'fail';
    }
  } catch(e) {
    r.response = 'Error: ' + e.message; r.status = 'fail';
  }
  render();
  save();
}

async function save() {
  // Surface a rejected save (422 bad shape / 503 no agent_router / 500 write
  // error) instead of swallowing it — a silent save reads as success while
  // nothing persisted. A network error (offline) stays silent: in-memory
  // state still works and the next save retries.
  try {
    const resp = await fetch('/_apx/eval/data', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(rows),
    });
    if (!resp.ok) {
      let msg = resp.status + ' ' + resp.statusText;
      try { const d = await resp.json(); msg = d.error || (d.detail && JSON.stringify(d.detail)) || msg; } catch {}
      const st = document.getElementById('status');
      if (st) st.textContent = 'Save failed: ' + msg;
      console.error('eval save failed:', msg);
    }
  } catch (e) {
    console.error('eval save error (offline?):', e);
  }
}

document.getElementById('run-all').addEventListener('click', async () => {
  const btn = document.getElementById('run-all');
  const fill = document.getElementById('progress-fill');
  const st = document.getElementById('status');
  btn.disabled = true;
  for (let i = 0; i < rows.length; i++) {
    st.textContent = `${i+1}/${rows.length}`;
    fill.style.width = (i / rows.length * 100) + '%';
    await runCase(i);
  }
  fill.style.width = '100%';
  const passed = rows.filter(r => r.status === 'pass').length;
  st.textContent = `${passed}/${rows.length} passed`;
  btn.disabled = false;
});

document.getElementById('reset').addEventListener('click', () => {
  rows.forEach(r => { r.status = 'pending'; r.response = ''; r.judge_verdict = null; r.judge_reason = null; });
  document.getElementById('progress-fill').style.width = '0%';
  document.getElementById('status').textContent = '';
  render(); save();
});

document.getElementById('add-btn').addEventListener('click', async () => {
  const q = document.getElementById('add-q').value.trim();
  const criterion = document.getElementById('add-criterion').value.trim();
  if (!q) return;
  rows.push({question: q, expected_judge: criterion, status: 'pending', response: ''});
  document.getElementById('add-q').value = '';
  document.getElementById('add-criterion').value = '';
  render(); await save();
  document.querySelector('.meta').textContent = rows.length + ' case' + (rows.length === 1 ? '' : 's');
});

render();

// Handle table selection from the parent shell's sidebar.
window.addEventListener('message', (e) => {
  if (e.data?.type !== 'apx:table-selected') return;
  const fqn = e.data.fqn || '';
  const table = fqn.split('.').pop();
  const suggestions = [
    `How many rows are in ${fqn}?`,
    `What are the most recent records in ${table}?`,
    `Show me a sample of data from ${table}`,
    `What are the column names and types in ${table}?`,
  ];
  // Pre-seed suggested eval cases for the selected table.
  let added = 0;
  for (const q of suggestions) {
    if (!rows.some(r => r.question === q)) {
      rows.push({question: q, expected_judge: '', status: 'pending', response: ''});
      added++;
    }
  }
  if (added) {
    render(); save();
    document.querySelector('.meta').textContent = rows.length + ' case' + (rows.length === 1 ? '' : 's');
  }
  // Scroll to + highlight the add-question box for any manual additions.
  const addQ = document.getElementById('add-q');
  if (addQ) {
    addQ.value = `Ask about ${table}: `;
    addQ.focus();
    document.querySelector('.add-section').scrollIntoView({behavior: 'smooth'});
  }
});
