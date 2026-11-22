// Legacy chat behavior after the history script. TABLE_JS stays inline
// in the page, ahead of this file, and provides the table helpers (#1052).
// ── Approvals (ASK policy human-in-the-loop) ──
// Backend: GET /_apx/approvals, POST /_apx/approvals/{id}/approve|deny (see _dev.py).
// ASK policy is non-blocking/retry-based: a gated tool is refused for the turn,
// the turn ends, the card surfaces here, and approving auto-submits a retry turn.
async function checkPendingApprovals() {
  let pending = [];
  try {
    const r = await fetch('/_apx/approvals');
    if (!r.ok) return;
    pending = await r.json();
    if (!Array.isArray(pending)) return;
  } catch { return; }
  for (const a of pending) {
    // One card per approval; skip if already rendered.
    if (document.querySelector(`[data-approval-id="${a.id}"]`)) continue;
    const card = document.createElement('div');
    card.className = 'approval-card';
    card.dataset.approvalId = a.id;
    card.style.cssText = 'margin:8px 0;padding:10px 12px;border:1px solid var(--apx-panel-2);border-radius:8px;background:var(--apx-panel-2)';
    const argsStr = JSON.stringify(a.arguments || {}, null, 0);
    card.innerHTML = `
      <div style="font-size:12px;color:var(--apx-warn);font-weight:600;margin-bottom:4px">⏸ Approval required: ${esc(a.tool_name)}</div>
      ${a.reason ? `<div style="font-size:11px;color:var(--apx-muted);margin-bottom:4px">${esc(a.reason)}</div>` : ''}
      <div style="font-size:11px;color:var(--apx-muted);font-family:monospace;margin-bottom:8px;word-break:break-all">${esc(argsStr.slice(0, 300))}</div>
      <button onclick="resolveApproval('${a.id}', true, this)" style="background:var(--apx-panel-2);color:var(--apx-ok);border:1px solid var(--apx-border);border-radius:5px;padding:4px 14px;cursor:pointer;font-size:12px;margin-right:8px">Approve</button>
      <button onclick="resolveApproval('${a.id}', false, this)" style="background:var(--apx-panel-2);color:var(--apx-err);border:1px solid var(--apx-border);border-radius:5px;padding:4px 14px;cursor:pointer;font-size:12px">Deny</button>`;
    chat.appendChild(card);
    chat.scrollTop = chat.scrollHeight;
  }
}

async function resolveApproval(id, approved, btn) {
  const card = btn.closest('.approval-card');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    const r = await fetch(`/_apx/approvals/${id}/${approved ? 'approve' : 'deny'}`, { method: 'POST' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (err) {
    if (card) card.querySelectorAll('button').forEach(b => b.disabled = false);
    addMsg('assistant', `Approval update failed: ${err.message}`, false);
    return;
  }
  if (card) card.remove();
  // Close the loop: tell the agent the decision so it retries (approved) or
  // moves on (denied) without the user retyping.
  inputEl.value = approved
    ? `I approved request ${id}. Please retry the action.`
    : `I denied request ${id}. Do not retry that action.`;
  form.requestSubmit();
}

async function promoteToEval(convId, label) {
  try {
    const items = await fetch(`/_apx/conversations/${convId}/items`).then(r => r.json());
    // Find the first user message and the first assistant response
    const userMsg = items.find(it => it.type === 'message' && it.data && it.data.role === 'user');
    const assistantMsg = items.find(it => it.type === 'message' && it.data && it.data.role === 'assistant');
    const question = userMsg ? (Array.isArray(userMsg.data.content)
      ? userMsg.data.content.map(p => p.text || '').join('') : userMsg.data.content || '') : label;
    const response = assistantMsg ? (Array.isArray(assistantMsg.data.content)
      ? assistantMsg.data.content.map(p => p.text || '').join('') : assistantMsg.data.content || '') : '';
    if (!evalLoaded) { await loadEvalCases(); }
    evalRows.push({ question: question.trim(), expected_judge: '', response: response.trim(), status: 'pending' });
    saveEvalCases();
    switchTab('eval', document.querySelectorAll('.panel-tabs button')[4]);
    renderEval();
  } catch(e) { alert('Failed to promote: ' + e.message); }
}

async function switchConversation(id) {
  devThreadId = id;
  sessionStorage.setItem(_THREAD_KEY, id);
  chat.innerHTML = '';
  history.length = 0;  // reset request payload so the switched-to conversation starts clean
  document.querySelectorAll('.conv-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === id);
  });
  try {
    const items = await fetch(`/_apx/conversations/${id}/items`).then(r => r.json());
    for (const item of items) {
      if (item.type !== 'message') continue;
      const role = item.data?.role;
      if (!role) continue;
      const blocks = item.data?.content || [];
      const text = blocks.map(b => b.text || b.content || '').join('');
      if (text) addMsg(role, text, false);
    }
  } catch {}
}

// ── Eval tab ──
let evalRows = [];
let evalLoaded = false;

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

function renderEval() {
  const el = document.getElementById('eval-cases');
  if (!evalRows.length) {
    el.innerHTML = '<div style="color:var(--apx-muted);font-size:12px;padding:20px 12px">No test cases. '
      + '<button onclick="reloadEvalCases()" style="background:none;border:none;color:var(--apx-accent);cursor:pointer;font-size:12px;text-decoration:underline">Load from MLflow</button></div>';
    return;
  }
  el.innerHTML = evalRows.map((r, i) => {
    const dot = r.status === 'pass' ? 'var(--apx-ok)' : r.status === 'fail' ? 'var(--apx-err)' : r.status === 'running' ? 'var(--apx-warn)' : 'var(--apx-border)';
    const anim = r.status === 'running' ? 'animation:pulse .8s infinite' : '';
    const ms = r.last_run_ms ? `<span style="font-size:10px;color:var(--apx-muted);font-family:monospace">${r.last_run_ms}ms</span>` : '';
    const traceLink = r.trace_id
      ? `<a href="/_apx/traces/${esc(r.trace_id)}" target="_blank" style="font-size:10px;color:var(--apx-accent);text-decoration:none">→ trace</a>`
      : '';
    const judgeBadge = r.judge_verdict
      ? `<span title="${esc(r.judge_reason || '')}" style="font-size:10px;font-weight:600;padding:1px 6px;border-radius:3px;`
        + (r.judge_verdict === 'PASS'
          ? `background:var(--apx-panel-2);color:var(--apx-ok)`
          : `background:var(--apx-panel-2);color:var(--apx-err)`)
        + `">judge: ${esc(r.judge_verdict)}</span>`
      : '';
    return `<div style="padding:8px 12px;border-bottom:1px solid var(--apx-border)" data-idx="${i}">
      <div style="display:flex;align-items:flex-start;gap:8px">
        <span style="width:8px;height:8px;border-radius:50%;background:${dot};${anim};flex-shrink:0;margin-top:4px;display:inline-block"></span>
        <span style="font-size:12px;color:var(--apx-muted);flex:1;cursor:pointer" onclick="toggleEvalResp(this)">${esc(r.question)}</span>
        ${judgeBadge} ${ms} ${traceLink}
        <button onclick="runEvalCase(${i})" title="Run this case" style="background:transparent;color:var(--apx-accent);border:none;cursor:pointer;padding:0 4px;font-size:13px" ${r.status === 'running' ? 'disabled' : ''}>▶</button>
        <button onclick="deleteEvalCase(${i})" title="Delete" style="background:transparent;color:var(--apx-muted);border:none;cursor:pointer;padding:0 4px;font-size:13px">✕</button>
      </div>
      <div style="margin:4px 0 0 16px">
        <input type="text" value="${esc(r.expected || '')}" placeholder="expected keywords (comma-separated)"
          onblur="updateExpected(${i}, this.value)"
          style="width:100%;background:transparent;border:none;border-bottom:1px solid var(--apx-border);color:var(--apx-muted);font-size:11px;padding:2px 0;outline:none" />
        <input type="text" value="${esc(r.expected_judge || '')}" placeholder="LLM judge criterion (e.g. 'answer mentions a temperature in fahrenheit') — overrides keywords"
          onblur="updateExpectedJudge(${i}, this.value)"
          style="width:100%;background:transparent;border:none;border-bottom:1px solid var(--apx-border);color:var(--apx-muted);font-size:11px;padding:2px 0;outline:none;margin-top:2px" />
      </div>
      ${r.response ? `<div style="font-size:11px;color:var(--apx-muted);margin:6px 0 2px 16px;white-space:pre-wrap;line-height:1.5;border-left:2px solid var(--apx-border);padding-left:8px" class="eval-resp">${esc(r.response.slice(0,600))}${r.response.length>600?'…':''}</div>` : ''}
      ${r.judge_reason ? `<div style="font-size:11px;color:var(--apx-muted);margin:2px 0 0 16px;font-style:italic">judge: ${esc(r.judge_reason)}</div>` : ''}
    </div>`;
  }).join('');
}

function toggleEvalResp(el) {
  // Question span → div(case) → response is the last child .eval-resp
  const wrapper = el.closest('[data-idx]');
  const resp = wrapper && wrapper.querySelector('.eval-resp');
  if (resp) resp.style.display = resp.style.display === 'none' ? '' : 'none';
}

async function reloadEvalCases() {
  evalLoaded = false;
  await loadEvalCases();
}

async function chatLabelAlign() {
  const judge = document.getElementById('eval-judge-name').value.trim();
  const st = document.getElementById('eval-align-status');
  if (!judge) { st.textContent = 'Enter a judge name.'; return; }
  document.getElementById('eval-align-btn').disabled = true;
  st.textContent = 'Running MemAlign… (may take a minute)';
  try {
    const r = await fetch('/_apx/eval/label-align', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({judge_name: judge}),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    st.innerHTML = `✓ ${d.guidelines?.length || 0} guidelines distilled from ${d.trace_count} traces`;
  } catch(e) {
    st.textContent = 'Error: ' + e.message;
  } finally {
    document.getElementById('eval-align-btn').disabled = false;
  }
}

async function loadEvalCases() {
  evalLoaded = true;
  document.getElementById('eval-cases').innerHTML = '<div style="color:var(--apx-muted);font-size:12px;padding:20px 12px">Loading from MLflow… (may take ~30s on cold warehouse)</div>';
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 90000);
    const r = await fetch('/_apx/eval/data', {signal: ctrl.signal});
    clearTimeout(t);
    const data = await r.json();
    evalRows = Array.isArray(data) ? data : [];
    renderEval();
  } catch(e) {
    document.getElementById('eval-cases').innerHTML = '<div style="color:var(--apx-err);font-size:12px;padding:12px">Failed to load: ' + e.message + '</div>';
  }
}

function _evalSaveBanner(msg) {
  // Single reusable banner as a sibling *before* #eval-cases, so renderEval()
  // (which rewrites #eval-cases.innerHTML) never wipes it. msg === null clears.
  const host = document.getElementById('eval-cases');
  if (!host || !host.parentNode) return;
  let b = document.getElementById('eval-save-err');
  if (msg === null) { if (b) b.remove(); return; }
  if (!b) {
    b = document.createElement('div');
    b.id = 'eval-save-err';
    b.style.cssText = 'color:var(--apx-err);font-size:12px;padding:8px 12px;background:var(--apx-panel-2);border-bottom:1px solid var(--apx-border)';
    host.parentNode.insertBefore(b, host);
  }
  b.textContent = 'Save failed: ' + msg;
}

let _evalSaveTimer = null;
function saveEvalCases() {
  // Debounce so per-keystroke edits don't hammer the disk.
  clearTimeout(_evalSaveTimer);
  _evalSaveTimer = setTimeout(async () => {
    try {
      const resp = await fetch('/_apx/eval/data', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(evalRows),
      });
      // Surface a rejected save (422/503/500) instead of swallowing it; a
      // silent save reads as success while nothing persisted.
      if (!resp.ok) {
        let msg = resp.status + ' ' + resp.statusText;
        try { const d = await resp.json(); msg = d.error || (d.detail && JSON.stringify(d.detail)) || msg; } catch {}
        _evalSaveBanner(msg);
        console.error('eval save failed:', msg);
      } else {
        _evalSaveBanner(null);
      }
    } catch { /* offline; in-memory state still works */ }
  }, 250);
}

function updateExpected(i, value) {
  if (!evalRows[i]) return;
  evalRows[i].expected = value;
  saveEvalCases();
}

function updateExpectedJudge(i, value) {
  if (!evalRows[i]) return;
  evalRows[i].expected_judge = value;
  saveEvalCases();
  // Persist back to MLflow as updated assessment rationale
  const tid = evalRows[i].trace_id;
  const val = evalRows[i].expected_pass !== undefined ? evalRows[i].expected_pass : true;
  if (tid) {
    // idempotency_key = trace_id so repeated edits update in place rather than
    // appending new assessments (attach_feedback matches on the key)
    fetch('/_apx/feedback', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({trace_id: tid, name: 'quality', value: val, comment: value, idempotency_key: tid}),
    }).then(() => fetch('/_apx/eval/cache/bust', {method:'POST'})).catch(() => {});
  }
}

function deleteEvalCase(i) {
  evalRows.splice(i, 1);
  renderEval();
  saveEvalCases();
}

async function runEvalCase(i) {
  const r = evalRows[i];
  r.status = 'running'; r.response = ''; r.trace_id = null; r.last_run_ms = null;
  renderEval();
  resetTrace();

  const t0 = performance.now();
  let text = '';
  let traceId = null;
  try {
    const resp = await fetch('/invocations', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({input: [{role:'user', content:r.question}], stream: true}),
    });
    if (!resp.ok) throw new Error(`${resp.status} ${await resp.text()}`);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream:true});
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const payload = JSON.parse(line.slice(6));
          if (payload.trace_id && !traceId) traceId = payload.trace_id;
          if (payload.type === 'response.output_text.delta' && payload.delta) { text += payload.delta; }
          else if (payload.type === 'response.output_item.done') {
            const item = payload.item || {};
            if (item.type === 'message' && Array.isArray(item.content))
              for (const p of item.content) if (p.type === 'output_text' && p.text) text += p.text;
          } else if (payload.type === 'response.completed' && !text) {
            const out = payload.response && payload.response.output;
            if (Array.isArray(out)) for (const it of out) if (it.type === 'message' && Array.isArray(it.content))
              for (const p of it.content) if (p.type === 'output_text' && p.text) text += p.text;
          }
        } catch {}
      }
    }
    r.response = text;
    // Judge takes precedence when set; otherwise keywords; otherwise length heuristic.
    if ((r.expected_judge || '').trim()) {
      try {
        const j = await fetch('/_apx/eval/judge', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ question: r.question, response: text, criterion: r.expected_judge }),
        });
        const jdata = await j.json();
        if (jdata.ok) {
          r.judge_verdict = jdata.verdict;
          r.judge_reason = jdata.reason;
          r.status = jdata.pass ? 'pass' : 'fail';
        } else {
          r.judge_verdict = 'ERROR';
          r.judge_reason = jdata.error || 'Judge call failed';
          r.status = 'fail';
        }
      } catch (jerr) {
        r.judge_verdict = 'ERROR';
        r.judge_reason = jerr.message;
        r.status = 'fail';
      }
    } else {
      r.judge_verdict = null;
      r.judge_reason = null;
      r.status = r.expected
        ? r.expected.split(/[,;]/).map(k=>k.trim().toLowerCase()).filter(Boolean).every(k=>text.toLowerCase().includes(k)) ? 'pass' : 'fail'
        : text.length > 10 ? 'pass' : 'fail';
    }
  } catch(e) { r.response = 'Error: ' + e.message; r.status = 'fail'; }
  r.trace_id = traceId;
  r.last_run_ms = Math.round(performance.now() - t0);
  finalizeTrace(traceId, r.status === 'fail' ? 'error' : 'completed');
  renderEval();
  saveEvalCases();
}

async function autoEvalResponse(question, response, traceId, msgDiv) {
  // Find an eval case whose question matches this interaction (fuzzy: startsWith)
  const q = question.trim().toLowerCase();
  const match = evalRows.find(r => q.startsWith(r.question.slice(0, 40).toLowerCase()) ||
                                   r.question.slice(0, 40).toLowerCase().startsWith(q));
  if (!match || !match.expected_judge) return;

  // Run the judge against this live response
  try {
    const j = await fetch('/_apx/eval/judge', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question, response, criterion: match.expected_judge}),
    });
    const d = await j.json();
    const pass = d.ok && d.pass;
    const badge = document.createElement('span');
    badge.title = d.reason || '';
    badge.style.cssText = 'font-size:10px;font-weight:600;padding:1px 6px;border-radius:3px;margin-left:6px;vertical-align:middle;cursor:default';
    badge.style.background = pass ? 'var(--apx-panel-2)' : 'var(--apx-panel-2)';
    badge.style.color = pass ? 'var(--apx-ok)' : 'var(--apx-err)';
    badge.textContent = pass ? '✓ eval pass' : '✗ eval fail';
    msgDiv.appendChild(badge);

    // Update the matching eval row so the tab reflects it too
    match.status = pass ? 'pass' : 'fail';
    match.response = response;
    match.judge_verdict = pass ? 'PASS' : 'FAIL';
    match.judge_reason = d.reason || '';
    match.trace_id = traceId || match.trace_id;
    renderEval();
  } catch(_) {}
}

async function runAllEvalCases() {
  const btn = document.getElementById('eval-run-all');
  const fill = document.getElementById('eval-progress-fill');
  const st   = document.getElementById('eval-status');
  btn.disabled = true;
  for (let i = 0; i < evalRows.length; i++) {
    st.textContent = `${i+1}/${evalRows.length}`;
    fill.style.width = (i / evalRows.length * 100) + '%';
    await runEvalCase(i);
  }
  fill.style.width = '100%';
  const passed = evalRows.filter(r => r.status === 'pass').length;
  st.textContent = `${passed}/${evalRows.length} passed`;
  btn.disabled = false;
}

document.getElementById('eval-run-all').addEventListener('click', async () => {
  if (!evalLoaded) await loadEvalCases();
  await runAllEvalCases();
});

document.getElementById('eval-reset').addEventListener('click', () => {
  evalRows.forEach(r => {
    r.status = 'pending'; r.response = '';
    r.trace_id = null; r.last_run_ms = null;
    r.judge_verdict = null; r.judge_reason = null;
  });
  document.getElementById('eval-progress-fill').style.width = '0%';
  document.getElementById('eval-status').textContent = '';
  renderEval();
  saveEvalCases();
});

document.getElementById('eval-add-btn').addEventListener('click', () => {
  const q = document.getElementById('eval-add-q').value.trim();
  if (!q) return;
  evalRows.push({question: q, expected: '', status: 'pending', response: ''});
  document.getElementById('eval-add-q').value = '';
  renderEval();
  saveEvalCases();
});

// ── State ──
const history = [];
let eventCounter = 0;
let events = [];
let eventsStarted = false;
let apxHost = '';
let apxMemoryTable = '';
(async () => {
  try {
    const d = await fetch('/_apx/workspace-context').then(r => r.json());
    apxHost = d.host || '';
    apxMemoryTable = d.memory_table || '';
    // Wire the ↗ table link to the actual Databricks UC explorer URL.
    if (apxHost && apxMemoryTable) {
      const link = document.querySelector('.cap-mem-link[data-mem-table]');
      if (link) {
        const parts = apxMemoryTable.split('.');
        if (parts.length === 3) {
          link.href = `${apxHost}/explore/data/${parts[0]}/${parts[1]}/${parts[2]}`;
          link.target = '_blank';
          link.rel = 'noreferrer';
        }
      }
    }
  } catch {}
  // Populate the landing page memory preview with recent stored memories.
  try {
    const mems = await fetch('/_apx/memories').then(r => r.json());
    const preview = document.getElementById('cap-mem-preview');
    if (preview && Array.isArray(mems) && mems.length) {
      preview.innerHTML = mems.slice(0, 3).map(m => {
        const ts = m.updated_at ? m.updated_at.slice(0, 10) : '';
        const tsSpan = ts ? `<span class="cap-mem-row-ts">${ts}</span>` : '';
        return `<div class="cap-mem-row" title="${m.content.replace(/"/g, '&quot;')}">${m.content}${tsSpan}</div>`;
      }).join('');
    }
  } catch {}
  // p50/p95 sparkline of the last 20 trace durations on empty-chat landing.
  try {
    const mount = document.getElementById('latency-spark');
    if (mount) {
      const r = await fetch('/_apx/traces/latency', {headers: {'Accept': 'application/json'}});
      if (r.ok) {
        const d = await r.json();
        const pts = Array.isArray(d.points) ? d.points.filter(v => typeof v === 'number' && isFinite(v)) : [];
        mount.hidden = false;
        const empty = document.getElementById('latency-spark-empty');
        const body = document.getElementById('latency-spark-body');
        if (!pts.length) {
          empty.hidden = false;
          body.hidden = true;
        } else {
          empty.hidden = true;
          body.hidden = false;
          const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
          const line = pts.map((v, i) => {
            const x = pts.length === 1 ? 120 : (i / (pts.length - 1)) * 240;
            const y = 34 - ((v - lo) / span) * 32;
            return x.toFixed(1) + ',' + y.toFixed(1);
          }).join(' ');
          document.getElementById('latency-spark-line').setAttribute('points', line);
          const fmt = (n) => n == null ? '—' : (n >= 1000 ? (n / 1000).toFixed(1) + 's' : Math.round(n) + 'ms');
          document.getElementById('latency-p50').textContent = 'p50 ' + fmt(d.p50_ms);
          document.getElementById('latency-p95').textContent = 'p95 ' + fmt(d.p95_ms);
        }
      }
    }
  } catch {}
})();

// Stable session key for the dev-UI conversation.  Stored in sessionStorage so
// a page refresh resumes the same server-side session (the user doesn't lose
// agent memory / conversation state).  A new tab always gets a fresh UUID.
const _THREAD_KEY = '_apx_dev_thread_id';
let devThreadId = sessionStorage.getItem(_THREAD_KEY);
if (!devThreadId) {
  devThreadId = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now().toString(36);
  sessionStorage.setItem(_THREAD_KEY, devThreadId);
}

// Load the conversation history list on page load (History is the default tab).
loadConversationHistory();
// Surface any approvals already pending when the UI is (re)opened.
checkPendingApprovals();

function fmt(v) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'string') return v.length > 600 ? v.slice(0, 600) + '\n…' : v;
  const s = JSON.stringify(v, null, 2);
  return s.length > 1200 ? s.slice(0, 1200) + '\n…' : s;
}

// ── Events ──
let eventsFilter = 'all';
document.getElementById('events-filter')?.addEventListener('click', e => {
  const button = e.target.closest('button[data-filter]');
  if (!button) return;
  eventsFilter = button.dataset.filter;
  document.querySelectorAll('#events-filter button').forEach(b => b.classList.toggle('active', b === button));
  document.querySelectorAll('#events-list .event').forEach(row => {
    row.style.display = eventsFilter === 'all' || row.dataset.kind === eventsFilter ? '' : 'none';
  });
});
function addEvent(type, title, subtitle, data) {
  if (!eventsStarted) { eventsList.innerHTML = ''; eventsList.classList.remove('empty-state'); eventsStarted = true; }
  const num = eventCounter++;
  const ev = { num, type, title, subtitle, data };
  events.push(ev);
  const div = document.createElement('div');
  div.className = 'event' + (type === 'tool-call' ? ' tool-call' : type === 'tool-result' ? ' tool-result' : type === 'tool-error' ? ' tool-error' : '');
  div.dataset.idx = events.length - 1;
  div.dataset.kind = type.startsWith('tool') ? 'tools' : type === 'step' ? 'steps' : 'messages';
  if (eventsFilter !== 'all' && div.dataset.kind !== eventsFilter) div.style.display = 'none';
  const icons = { user: '👤', assistant: '🤖', 'tool-call': '⚡', 'tool-result': '✓', 'tool-error': '✗', step: '▸' };
  // Label tool rows clearly as call vs response (icons alone aren't obvious).
  // The tool name (when present and not the generic "tool") is appended.
  const kindLabel = { 'tool-call': 'tool call', 'tool-result': 'tool response', 'tool-error': 'tool error' }[type] || '';
  const shownTitle = kindLabel
    ? (title && title !== 'tool' ? `${kindLabel} · ${title}` : kindLabel)
    : title;
  // Built with textContent: event text (tool output included) is never parsed as markup.
  const part = (cls, text, tag = 'span') => { const el = document.createElement(tag); el.className = cls; el.textContent = text; return el; };
  const bodyEl = part('event-body', '', 'div');
  bodyEl.appendChild(part('event-title', shownTitle, 'div'));
  if (subtitle) bodyEl.appendChild(part('event-sub', String(subtitle), 'div'));
  div.append(part('event-num', '#' + num), part('event-icon', icons[type] || '•'), bodyEl);
  div.onclick = () => showDetail(ev, div);
  eventsList.appendChild(div);
  eventsList.scrollTop = eventsList.scrollHeight;
  return ev;
}

// ── Tool event grouping ──
// Each tool call + its response are grouped into one collapsible block keyed by
// call_id (request and response together), so you read "ran X → got Y" as a
// unit instead of all-calls-then-all-responses interleaved. Non-tool events
// (user/assistant) stay flat rows via addEvent. Memory tools (recall/remember/
// forget) share one "memory" card regardless of how many calls there are.
// Reset per send.
const toolGroups = {};     // groupId -> { body }
const memCallBodies = {};  // call_id  -> sub-body within the shared memory card
const MEM_GROUP_ID = '__apx_memory__';

function isMemoryTool(name) {
  if (typeof name !== 'string') return false;
  const n = name.toLowerCase();
  return ['recall','remember','forget'].some(k => n === k || n.endsWith('_' + k));
}
function memOpLabel(name) {
  const n = (name || '').toLowerCase();
  if (n === 'recall'   || n.endsWith('_recall'))   return 'recall';
  if (n === 'remember' || n.endsWith('_remember')) return 'remember';
  if (n === 'forget'   || n.endsWith('_forget'))   return 'forget';
  return name;
}
function isRecallTool(name) {
  return typeof name === 'string' && (name === 'recall' || name.endsWith('_recall'));
}
const RECALL_TOP_N = 3;
function parseMemoryLines(text) {
  if (!text || text.trim() === 'No memories found.') return [];
  return text.split('\n')
    .filter(l => l.trim().startsWith('-'))
    .map(l => {
      const m = l.match(/^\s*-\s*\[score=([\d.]+)\]\s*(.*)/);
      return m ? { score: parseFloat(m[1]), content: m[2] }
               : { score: null, content: l.replace(/^\s*-\s*/, '').trim() };
    })
    .filter(item => item.content);
}

function addToolCall(groupId, name, reqText, reqData) {
  if (!eventsStarted) { eventsList.innerHTML = ''; eventsList.classList.remove('empty-state'); eventsStarted = true; }
  const num = eventCounter++;
  const reqEv = { num, type: 'tool-call', title: name || 'tool', subtitle: reqText, data: reqData };
  events.push(reqEv);

  // Memory tools share one card.
  if (isMemoryTool(name)) {
    let memGroup = toolGroups[MEM_GROUP_ID];
    if (!memGroup) {
      const group = document.createElement('div');
      group.className = 'event tool-group open';
      const head = document.createElement('div');
      head.className = 'tg-head';
      head.innerHTML = `<span class="event-num">#${num}</span><span class="event-icon">🧠</span>`
        + `<span class="tg-name">memory</span><span class="tg-hint">request + response</span><span class="tg-caret">▾</span>`;
      head.onclick = () => group.classList.toggle('open');
      const body = document.createElement('div');
      body.className = 'tg-body';
      group.appendChild(head);
      group.appendChild(body);
      eventsList.appendChild(group);
      toolGroups[MEM_GROUP_ID] = { body };
      memGroup = toolGroups[MEM_GROUP_ID];
    }
    // Each call gets its own sub-container so its response lands in the right spot.
    const sub = document.createElement('div');
    sub.style.cssText = 'display: contents;';
    memGroup.body.appendChild(sub);
    const reqRow = document.createElement('div');
    reqRow.className = 'tg-part';
    reqRow.innerHTML = `<span class="tg-label">${memOpLabel(name)}</span>`
      + `<span class="tg-val">${esc(reqText || '')}</span>`;
    reqRow.onclick = () => showDetail(reqEv, null);
    sub.appendChild(reqRow);
    memCallBodies[groupId] = { sub, name };
    eventsList.scrollTop = eventsList.scrollHeight;
    return reqEv;
  }

  const group = document.createElement('div');
  group.className = 'event tool-group open';
  group.dataset.idx = events.length - 1;
  const head = document.createElement('div');
  head.className = 'tg-head';
  head.innerHTML = `<span class="event-num">#${num}</span><span class="event-icon">⚡</span>`
    + `<span class="tg-name">${esc(name || 'tool')}</span><span class="tg-hint">request + response</span><span class="tg-caret">▾</span>`;
  head.onclick = () => group.classList.toggle('open');
  const body = document.createElement('div');
  body.className = 'tg-body';
  const reqRow = document.createElement('div');
  reqRow.className = 'tg-part';
  reqRow.innerHTML = '<span class="tg-label">request</span>'
    + `<span class="tg-val">${esc(reqText || '')}</span>`;
  reqRow.onclick = () => showDetail(reqEv, null);
  body.appendChild(reqRow);
  group.appendChild(head);
  group.appendChild(body);
  eventsList.appendChild(group);
  eventsList.scrollTop = eventsList.scrollHeight;
  toolGroups[groupId] = { body, callEv: reqEv };
  return reqEv;
}

function _appendMemoryUcLink(container) {
  if (!apxHost || !apxMemoryTable) return;
  const parts = apxMemoryTable.split('.');
  const ucUrl = parts.length === 3
    ? `${apxHost}/explore/data/${parts[0]}/${parts[1]}/${parts[2]}`
    : `${apxHost}/explore/data`;
  const a = document.createElement('a');
  a.href = ucUrl; a.target = '_blank';
  a.textContent = `${apxMemoryTable} ↗`;
  a.style.cssText = 'font-size:11px;color:var(--apx-muted);font-family:ui-monospace,monospace;text-decoration:none;';
  a.onmouseover = () => a.style.color = 'var(--apx-accent)';
  a.onmouseout  = () => a.style.color = 'var(--apx-muted)';
  a.onclick = e => e.stopPropagation();
  container.appendChild(a);
}

function addToolResponse(groupId, name, respText, respData, isErr) {
  // Memory tool response — route into the shared memory card's sub-container.
  if (memCallBodies[groupId]) {
    const { sub, name: toolName } = memCallBodies[groupId];
    const ev = { num: '', type: isErr ? 'tool-error' : 'tool-result',
                 title: toolName || 'tool', subtitle: respText, data: respData };
    events.push(ev);
    const fullText = (respData && (respData.output || respData.result)) || respText || '';
    // Recall: show top-N items with "View all" toggle + UC link.
    if (!isErr && isRecallTool(toolName)) {
      const items = parseMemoryLines(fullText);
      if (items.length > 0) {
        const row = document.createElement('div');
        row.className = 'tg-part';
        row.style.cssText = 'cursor:pointer;align-items:flex-start;';
        row.onclick = () => showDetail(ev, null);
        const lbl = document.createElement('span');
        lbl.className = 'tg-label'; lbl.textContent = 'found';
        row.appendChild(lbl);
        const valDiv = document.createElement('div');
        valDiv.style.cssText = 'flex:1;min-width:0;display:flex;flex-direction:column;gap:2px;';
        const itemStyle = 'font-size:12px;color:var(--apx-muted);font-family:ui-monospace,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
        items.slice(0, RECALL_TOP_N).forEach(item => {
          const el = document.createElement('div');
          el.style.cssText = itemStyle; el.title = item.content;
          el.textContent = (item.score != null ? `[${item.score.toFixed(2)}] ` : '') + item.content;
          valDiv.appendChild(el);
        });
        const rest = items.slice(RECALL_TOP_N);
        if (rest.length > 0) {
          const restDiv = document.createElement('div');
          restDiv.style.cssText = 'display:none;flex-direction:column;gap:2px;';
          rest.forEach(item => {
            const el = document.createElement('div');
            el.style.cssText = itemStyle; el.title = item.content;
            el.textContent = (item.score != null ? `[${item.score.toFixed(2)}] ` : '') + item.content;
            restDiv.appendChild(el);
          });
          valDiv.appendChild(restDiv);
          const btn = document.createElement('button');
          btn.textContent = `View all (${items.length}) ▸`;
          btn.style.cssText = 'background:none;border:none;color:var(--apx-accent);font-size:11px;cursor:pointer;padding:2px 0;text-align:left;font-family:ui-monospace,monospace;';
          btn.onclick = e => {
            e.stopPropagation();
            const open = restDiv.style.display !== 'none';
            restDiv.style.display = open ? 'none' : '';
            btn.textContent = open ? `View all (${items.length}) ▸` : '▾ Collapse';
          };
          valDiv.appendChild(btn);
        } else {
          const c = document.createElement('div');
          c.style.cssText = 'font-size:11px;color:var(--apx-muted);font-family:ui-monospace,monospace;';
          c.textContent = `${items.length} memor${items.length === 1 ? 'y' : 'ies'}`;
          valDiv.appendChild(c);
        }
        _appendMemoryUcLink(valDiv);
        row.appendChild(valDiv);
        sub.appendChild(row);
        eventsList.scrollTop = eventsList.scrollHeight;
        return ev;
      }
    }
    // remember/forget: plain response row.
    const row = document.createElement('div');
    row.className = 'tg-part' + (isErr ? ' err' : '');
    row.innerHTML = `<span class="tg-label">${isErr ? 'error' : 'saved'}</span>`
      + `<span class="tg-val">${esc(respText || '')}</span>`;
    row.onclick = () => showDetail(ev, null);
    sub.appendChild(row);
    eventsList.scrollTop = eventsList.scrollHeight;
    return ev;
  }

  const g = toolGroups[groupId];
  // Unmatched response (no preceding call captured) → fall back to a flat row.
  if (!g) return addEvent(isErr ? 'tool-error' : 'tool-result', name || 'tool', respText, respData);
  const ev = { num: '', type: isErr ? 'tool-error' : 'tool-result',
               title: name || 'tool', subtitle: respText, data: respData };
  events.push(ev);
  // For SQL tools: update the request row and call event detail to show SQL.
  // respData.result may be a parsed object (trace replay) or string (live stream).
  let respDisplay = respText || '';
  if (!isErr && respData) {
    const rawResult = respData.output || respData.result;
    if (rawResult) {
      try {
        const parsed = typeof rawResult === 'string' ? JSON.parse(rawResult) : rawResult;
        if (parsed._sql) {
          // Update compact request row with SQL preview
          const reqPart = g.body.querySelector('.tg-part');
          if (reqPart) {
            const valEl = reqPart.querySelector('.tg-val');
            if (valEl) valEl.textContent = parsed._sql.replace(/\s+/g, ' ').trim().slice(0, 160);
          }
          // Inject SQL into the call event so the detail panel can show it
          if (g.callEv && g.callEv.data) g.callEv.data._sql = parsed._sql;
          // Build a clean response summary: "N rows · query took Xs"
          const rowCount = Array.isArray(parsed.data) ? parsed.data.length : null;
          const timingRaw = parsed._timing || '';
          const timing = timingRaw.replace(/^\[|\]$/g, '');
          respDisplay = [
            rowCount != null ? `${rowCount} row${rowCount !== 1 ? 's' : ''}` : null,
            timing || null
          ].filter(Boolean).join(' · ');
        }
      } catch {}
    }
  }
  const row = document.createElement('div');
  row.className = 'tg-part' + (isErr ? ' err' : '');
  row.innerHTML = `<span class="tg-label">${isErr ? 'error' : 'response'}</span>`
    + `<span class="tg-val">${esc(respDisplay)}</span>`;
  row.onclick = () => showDetail(ev, null);
  g.body.appendChild(row);
  eventsList.scrollTop = eventsList.scrollHeight;
  return ev;
}

function showDetail(ev, el) {
  document.querySelectorAll('.event.selected').forEach(e => e.classList.remove('selected'));
  if (el) el.classList.add('selected');
  detailTitle.textContent = `#${ev.num} ${ev.type}`;
  let html = '';
  if (ev.data) {
    if (ev.type === 'assistant' && ev.data.content) {
      // Render assistant responses as markdown in the detail panel.
      try { html = DOMPurify.sanitize(marked.parse(ev.data.content)); }
      catch { html = `<pre>${esc(ev.data.content)}</pre>`; }
    } else {
      // For tool events use the same formatters as inline steps: SQL gets a
      // code block, row arrays get a table — everything else falls back to <pre>.
      const isCallKey = k => k === 'arguments' || k === 'inputs' || k === 'args';
      const isRespKey = k => k === 'result' || k === 'outputs' || k === 'output';
      // For tool-call events: show SQL as the request when available (set
      // retroactively by addToolResponse once the result arrives).
      if (ev.type === 'tool-call' && ev.data._sql) {
        html = `<div class="label">request</div>${fmtSql(ev.data._sql)}`;
      } else {
        for (const [k, v] of Object.entries(ev.data)) {
          if (k === '_sql') continue;  // already shown above or handled in fmtResp
          const raw = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
          const body = isCallKey(k) ? fmtReq(raw) : isRespKey(k) ? fmtResp(raw) : `<pre>${esc(raw)}</pre>`;
          html += `<div class="label">${esc(k)}</div>${body}`;
        }
      }
    }
  }
  detailBody.innerHTML = html;
  detailPanel.classList.add('open');
  // Auto-switch to events tab (4th button: History, Tools, Trace, Events, Eval)
  switchTab('events', document.querySelectorAll('.panel-tabs button')[3]);
}

function closeDetail() {
  detailPanel.classList.remove('open');
  document.querySelectorAll('.event.selected').forEach(e => e.classList.remove('selected'));
}

// ── Chat ──
function renderAssistantInto(el, text) {
  // Sanitized markdown → HTML for assistant messages. marked parses, DOMPurify
  // strips anything unsafe (escape-by-default; the model never injects raw HTML).
  try {
    el.innerHTML = DOMPurify.sanitize(marked.parse(text || ''));
    // Block-level HTML now owns the layout — turn off the pre-wrap that the
    // plain-text fallback relies on, otherwise marked's inter-block newline
    // text-nodes render as visible blank lines (double-spaced output).
    el.style.whiteSpace = 'normal';
  } catch (e) {
    el.textContent = text;       // never break the chat on a render error
    el.style.whiteSpace = '';    // revert to the CSS pre-wrap for plain text
  }
}
function addMsg(role, text, streaming) {
  const div = document.createElement('div');
  div.className = `msg ${role}${streaming ? ' streaming' : ''}`;
  if (role === 'assistant') renderAssistantInto(div, text);
  else div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function attachAssistantTrace(afterEl, traceId, status) {
  if (!traceId || !afterEl) return;
  const prior = afterEl.nextElementSibling;
  if (prior && prior.classList.contains('trace-inline')) prior.remove();
  const row = document.createElement('div');
  row.className = 'trace-inline';
  const shortId = traceId.length > 18 ? traceId.slice(0, 18) + '...' : traceId;
  const state = status === 'error' ? 'errored' : 'captured';
  row.innerHTML =
    `<span>MLflow trace ${state}</span>` +
    `<button type="button" title="Show this trace in the APX trace panel">` +
    `<code>${escHtml(shortId)}</code></button>` +
    `<a href="/_apx/traces/${encodeURIComponent(traceId)}" target="_blank" title="Open full trace">full</a>`;
  row.querySelector('button').onclick = () => {
    switchTab('trace', document.querySelectorAll('.panel-tabs button')[2]);
    finalizeTrace(traceId, status, { emitEvents: false });
  };
  afterEl.after(row);
}

// ── Trace tab: live span bubbles ──
const traceBody = document.getElementById('trace-body');
const traceStatusEl = document.getElementById('trace-status');
const traceLinkEl = document.getElementById('trace-link');

function escHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function extractMsg(value) {
  if (value == null) return '';
  if (typeof value === 'string') {
    try { return extractMsg(JSON.parse(value)); } catch { return value.slice(0, 400); }
  }
  if (typeof value === 'number') return String(value);
  if (Array.isArray(value)) {
    // Responses-API output array: find the last message item's text.
    const msgs = value.filter(m => m && typeof m === 'object' && m.type === 'message');
    if (msgs.length) return extractMsg(msgs[msgs.length - 1].content);
    // Generic: last item with a content field.
    for (let i = value.length - 1; i >= 0; i--) {
      const m = value[i];
      if (m && typeof m === 'object' && 'content' in m) return extractMsg(m.content);
    }
    return value.map(extractMsg).filter(Boolean).join(', ').slice(0, 300);
  }
  if (typeof value === 'object') {
    // Responses-API output_text block.
    if (value.type === 'output_text' && value.text) return value.text.slice(0, 400);
    // Grounded-tool result (e.g. knowledge_assistant): show the answer + a
    // citation count, not the raw {question, answer, citations} dict.
    if ('answer' in value) {
      const n = Array.isArray(value.citations) ? value.citations.length : 0;
      return String(value.answer).slice(0, 400) + (n ? ` [${n} citation${n > 1 ? 's' : ''}]` : '');
    }
    for (const k of ['content', 'text', 'output_text', 'message', 'messages', 'choices', 'output', 'input']) {
      if (k in value && value[k] != null) return extractMsg(value[k]);
    }
    return Object.entries(value).filter(([, v]) => v != null && v !== '')
      .map(([k, v]) => typeof v === 'string'
        ? (v.length > 60 ? `${k}: ${v.slice(0, 60)}…` : `${k}: ${v}`)
        : `${k}: ${typeof v === 'number' ? v : JSON.stringify(v).slice(0, 40)}`)
      .join('\n');
  }
  return String(value).slice(0, 300);
}


function resetTrace() {
  traceBody.innerHTML = '<div style="color:var(--apx-muted);font-size:12px;padding:8px 0">Running…</div>';
  traceStatusEl.textContent = 'running…';
  traceLinkEl.style.display = 'none';
}

async function finalizeTrace(traceId, status, opts) {
  opts = opts || {};
  traceStatusEl.textContent = status === 'error' ? 'errored' : 'done';
  if (!traceId) {
    // ResponsesAgent doesn't emit trace_id in the stream — fall back to the
    // most recent trace logged in this experiment.
    try {
      await new Promise(r => setTimeout(r, 500)); // let MLflow flush the trace
      const r = await fetch('/_apx/traces?fmt=json&max=1');
      const rows = await r.json();
      if (rows && rows.length) traceId = rows[0].trace_id;
    } catch {}
  }
  if (!traceId) {
    traceBody.innerHTML = '<div style="color:var(--apx-muted);font-size:12px;padding:8px 0">No trace found.</div>';
    return null;
  }
  traceLinkEl.href = `/_apx/traces/${traceId}`;
  traceLinkEl.style.display = 'inline';
  // Load and render spans inline. ``mlflow.langchain.autolog()`` writes its
  // spans as artifacts on a background thread, so the first fetch can race
  // and return ``[]`` even when the trace exists. Retry once after 1.5s before
  // declaring "no spans".
  try {
    let data;
    for (let attempt = 0; attempt < 2; attempt++) {
      const r = await fetch(`/_apx/traces/${traceId}?fmt=json`);
      data = await r.json();
      if (data.spans && data.spans.length) break;
      if (data.error) break;
      await new Promise(res => setTimeout(res, 1500));
    }
    if (data.error || !data.spans || !data.spans.length) {
      traceBody.innerHTML = `<div style="color:var(--apx-muted);font-size:12px;padding:8px 0">${data.error || 'No spans.'}</div>`;
      return traceId;
    }
    traceBody.innerHTML = '';
    const SPAN_COLORS = {LLM:'var(--apx-accent)',TOOL:'var(--apx-warn)',CHAIN:'var(--apx-text)',AGENT:'var(--apx-accent)',OTHER:'var(--apx-muted)'};
    function spanTypeShort(t) {
      t = (t||'').toUpperCase();
      if (['LLM','CHAT_MODEL','EMBEDDING'].includes(t)) return 'LLM';
      if (['TOOL','RETRIEVER'].includes(t)) return 'TOOL';
      if (t === 'CHAIN') return 'CHAIN';
      if (t === 'AGENT') return 'AGENT';
      return 'OTHER';
    }

    // ── Summary view: LLM + TOOL spans in start-time order ──
    // Normalization in _serialize_trace_spans ensures Responses-API traces
    // already have CHAT_MODEL spans and synthetic TOOL children, so one path
    // handles all formats.
    const ordered = [...data.spans].sort((a,b) => (a.start_time_ns||0)-(b.start_time_ns||0));
    // Framework-plumbing CHAIN spans that aren't meaningful sub-agents. Named
    // CHAIN/AGENT spans (router, knowledge_assistant, …) ARE sub-agent
    // boundaries and belong in the summary alongside LLM + TOOL steps.
    const CHAIN_NOISE = new Set(['LangGraph','RunnableCallable','model','tools','compile_to_langgraph']);
    function isSubAgent(s) {
      const t = spanTypeShort(s.span_type);
      const n = s.name || '';
      // Real sub-agents are graph-node names (bare identifiers: router,
      // knowledge_assistant, …). Exclude framework wrappers: the noise set,
      // dotted names (ApxResponsesAgent.invoke), and request/transport spans
      // with spaces or slashes (POST /responses).
      return (t === 'AGENT' || t === 'CHAIN') && n
        && !CHAIN_NOISE.has(n) && !n.includes('.') && !n.includes(' ') && !n.includes('/');
    }
    const keySpans = ordered.filter(s => {
      const t = spanTypeShort(s.span_type);
      return t === 'LLM' || t === 'TOOL' || isSubAgent(s);
    });

    function makeStepCard(typeLabel, color, name, dur, isErr) {
      const card = document.createElement('div');
      card.className = 'span-step';
      card.style.cssText = 'margin-bottom:6px;';
      card.innerHTML =
        `<div class="step-header">` +
        `<span style="font-size:10px;font-weight:700;padding:1px 5px;border-radius:3px;background:${color}22;color:${color}">${typeLabel}</span>` +
        `<span class="who" style="color:${color};font-size:12px;font-weight:600;margin-left:6px">${escHtml(name)}</span>` +
        (isErr ? `<span style="color:var(--apx-err);font-size:10px;margin-left:6px">ERR</span>` : '') +
        (dur ? `<span class="dur" style="margin-left:auto">${dur}</span>` : '') +
        `</div>`;
      return card;
    }
    function addBubble(card, val, cls) {
      const msg = extractMsg(val);
      if (!msg) return;
      const b = document.createElement('div');
      b.className = `span-bubble ${cls}`;
      b.style.cssText = 'font-size:11px;margin-top:4px;';
      b.textContent = msg.slice(0, 200) + (msg.length > 200 ? '…' : '');
      card.appendChild(b);
    }

    if (!keySpans.length) {
      traceBody.innerHTML = '<div style="color:var(--apx-muted);font-size:12px;padding:8px 0">No tool or model spans found.</div>';
    }
    for (const s of keySpans) {
      const type = spanTypeShort(s.span_type);
      const sub = isSubAgent(s);
      const label = sub ? 'SUB-AGENT' : type;
      const color = sub ? SPAN_COLORS.AGENT : (SPAN_COLORS[type] || 'var(--apx-muted)');
      const dur = s.duration_ms != null ? `${Math.round(s.duration_ms)}ms` : '';
      const isErr = (s.status||'').toUpperCase().includes('ERR');
      const card = makeStepCard(label, color, s.name, dur, isErr);
      if (sub) {
        // Sub-agent boundary: show only what it was asked, not its full plumbing.
        addBubble(card, s.inputs, 'agent-ask');
      } else {
        addBubble(card, s.inputs,  type === 'TOOL' ? 'tool-in' : 'agent-ask');
        addBubble(card, s.outputs, type === 'TOOL' ? 'tool-out' : 'llm-reply');
      }
      traceBody.appendChild(card);
    }

    if (!keySpans.length) {
      traceBody.innerHTML = '<div style="color:var(--apx-muted);font-size:12px;padding:8px 0">No LLM or tool spans found.</div>';
    }

    // ── Full tree (collapsed by default) ──
    const toggle = document.createElement('button');
    toggle.textContent = '▸ Full trace';
    toggle.style.cssText = 'margin-top:8px;background:none;border:1px solid var(--apx-border);color:var(--apx-muted);font-size:11px;padding:2px 8px;border-radius:3px;cursor:pointer;';
    const treeDiv = document.createElement('div');
    treeDiv.style.display = 'none';
    toggle.onclick = () => {
      const open = treeDiv.style.display !== 'none';
      treeDiv.style.display = open ? 'none' : 'block';
      toggle.textContent = open ? '▸ Full trace' : '▾ Full trace';
    };
    traceBody.appendChild(toggle);
    traceBody.appendChild(treeDiv);

    // Build parent→children for full tree
    const byParent = {};
    const roots = [];
    for (const s of data.spans) {
      if (s.parent_id) (byParent[s.parent_id] = byParent[s.parent_id] || []).push(s);
      else roots.push(s);
    }
    function renderSpanNode(s, depth) {
      const type = spanTypeShort(s.span_type);
      const color = SPAN_COLORS[type] || 'var(--apx-muted)';
      const dur = s.duration_ms != null ? `${Math.round(s.duration_ms)}ms` : '';
      const isErr = (s.status||'').toUpperCase().includes('ERR');
      const wrap = document.createElement('div');
      wrap.style.cssText = `padding-left:${depth*12}px;margin-bottom:2px;`;
      const card = document.createElement('div');
      card.className = 'span-step';
      card.innerHTML =
        `<div class="step-header">` +
        `<span style="font-size:9px;font-weight:700;padding:1px 4px;border-radius:2px;background:${color}22;color:${color}">${type}</span>` +
        `<span class="who" style="color:${color};font-size:11px;font-weight:600;margin-left:5px">${escHtml(s.name)}</span>` +
        (isErr ? `<span style="color:var(--apx-err);font-size:9px;margin-left:5px">ERR</span>` : '') +
        (dur ? `<span class="dur" style="margin-left:auto;font-size:10px">${dur}</span>` : '') +
        `</div>`;
      wrap.appendChild(card);
      for (const child of (byParent[s.span_id] || [])) {
        wrap.appendChild(renderSpanNode(child, depth + 1));
      }
      return wrap;
    }
    for (const root of roots) treeDiv.appendChild(renderSpanNode(root, 0));

    // Surface tool calls + results in the Events panel so it stops only
    // showing the user/assistant "one side" of the conversation. Gated on
    // ``opts.emitEvents`` so the page-load autoload (which fetches the most
    // recent historical trace) doesn't pollute the events list before the
    // user has even sent a message.
    if (opts.emitEvents) {
      // Spans come back roughly in start order; preserve that for the
      // events list so call/result interleave correctly across multi-tool runs.
      const orderedSpans = [...data.spans].sort(
        (a, b) => (a.start_time_ns || 0) - (b.start_time_ns || 0)
      );
      for (const s of orderedSpans) {
        if ((s.span_type || '').toUpperCase() !== 'TOOL') continue;
        const args = s.inputs || {};
        const isErr = (s.status || '').toUpperCase().includes('ERR');
        const result = s.outputs;
        const dur = s.duration_ms != null ? `${s.duration_ms}ms` : '';
        const gid = s.span_id || s.name;
        addToolCall(gid, s.name, fmt(args).slice(0, 120), { arguments: args });
        addToolResponse(gid, s.name, dur, { result: result }, isErr);
      }
    }
  } catch(e) {
    traceBody.innerHTML = `<div style="color:var(--apx-err);font-size:12px">${escHtml(e.message)}</div>`;
  }
  return traceId;
}

function addToolPills(trace) {
  const container = document.createElement('div');
  container.className = 'tool-pills';
  let _pi = 0;
  for (const t of trace) {
    const gid = 'pill-' + (_pi++);
    const isErr = t.result && typeof t.result === 'object' && 'error' in t.result;
    const call = document.createElement('span');
    call.className = 'tool-pill call';
    call.innerHTML = `<span class="icon">⚡</span>${t.name}`;
    call.dataset.tip = JSON.stringify(t.args, null, 2);
    call.onmouseenter = showTip;
    call.onmouseleave = hideTip;
    const callEv = addToolCall(gid, t.name, fmt(t.args).slice(0, 120), { arguments: t.args });
    call.onclick = () => showDetail(callEv, null);
    container.appendChild(call);
    const res = document.createElement('span');
    res.className = `tool-pill ${isErr ? 'error' : 'result'}`;
    res.innerHTML = `<span class="icon">${isErr ? '✗' : '✓'}</span>${t.name}<span class="ms">${t.ms}ms</span>`;
    res.dataset.tip = fmt(t.result);
    res.onmouseenter = showTip;
    res.onmouseleave = hideTip;
    const resEv = addToolResponse(gid, t.name, `${t.ms}ms`, { result: t.result }, isErr);
    res.onclick = () => showDetail(resEv, null);
    container.appendChild(res);
  }
  chat.appendChild(container);
  chat.scrollTop = chat.scrollHeight;
}

// ── Tool detail formatters ──
// SQL keyword highlighter.
function sqlHL(sql) {
  const kws = /\b(SELECT|FROM|WHERE|AND|OR|NOT|NULL|TRUE|FALSE|AS|IN|ON|JOIN|LEFT|RIGHT|INNER|ORDER|BY|LIMIT|GROUP|HAVING|DISTINCT|CASE|WHEN|THEN|ELSE|END|INSERT|UPDATE|DELETE|CREATE|DROP|TABLE|VIEW|WITH|IS|LIKE|BETWEEN|EXISTS|ALL|ANY|UNION|VALUES|SET|INTO|OUTER|CROSS|FULL|ASC|DESC|CAST|OVER|PARTITION|NULLIF|COALESCE|COUNT|SUM|AVG|MIN|MAX|RANK)\b/gi;
  return sql.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/('(?:[^']|'')*')/g, s => `<span style="color:var(--apx-text)">${s}</span>`)
    .replace(kws, k => `<span style="color:var(--apx-text);font-weight:600">${k}</span>`);
}
function fmtSql(sql) {
  return `<pre style="background:var(--apx-border);font-family:ui-monospace,monospace;font-size:11px;line-height:1.6;padding:10px 12px;border-radius:4px;overflow:auto;margin:0">${sqlHL(sql)}</pre>`;
}
function fmtTable(rows) {
  if (!rows || !rows.length) return '<div style="color:var(--apx-muted);font-size:12px;padding:8px">No rows returned.</div>';
  const cols = Object.keys(rows[0]);
  const th = cols.map(c => `<th style="padding:4px 8px;text-align:left;border-bottom:1px solid var(--apx-border);color:var(--apx-text);font-weight:500;white-space:nowrap">${esc(c)}</th>`).join('');
  const trs = rows.map(r =>
    `<tr>${cols.map(c => `<td style="padding:3px 8px;border-bottom:1px solid var(--apx-border);color:var(--apx-muted);white-space:nowrap">${esc(String(r[c] ?? ''))}</td>`).join('')}</tr>`
  ).join('');
  return `<div style="overflow:auto"><table style="border-collapse:collapse;font-family:ui-monospace,monospace;font-size:11px;width:100%"><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table><div style="color:var(--apx-muted);font-size:11px;padding:4px 8px">${rows.length} row${rows.length !== 1 ? 's' : ''}</div></div>`;
}
function fmtReq(rawStr) {
  try {
    const obj = JSON.parse(rawStr);
    const pretty = JSON.stringify(obj, null, 2);
    return `<pre style="background:var(--apx-border);font-family:ui-monospace,monospace;font-size:11px;line-height:1.5;padding:10px 12px;border-radius:4px;overflow:auto;margin:0;color:var(--apx-muted)">${esc(pretty)}</pre>`;
  } catch {}
  return `<pre class="step-detail-pre">${esc(rawStr)}</pre>`;
}
function fmtResp(rawStr) {
  try {
    const obj = JSON.parse(rawStr);
    if (obj._sql || Array.isArray(obj.data)) {
      let html = '';
      if (obj._sql) {
        // UC function link header (SQL itself is shown in the Request section)
        const ucMatch = obj._sql.match(/FROM\s+([\w]+)\.([\w]+)\.([\w]+)\s*\(/i);
        if (ucMatch) {
          const [, cat, schema, fn] = ucMatch;
          const fqn = `${cat}.${schema}.${fn}`;
          // Link to schema page — function URLs return "table not found" in UC explorer
          const ucUrl = apxHost ? `${apxHost}/explore/data/${cat}/${schema}` : null;
          const fnLink = ucUrl
            ? `<a href="${ucUrl}" target="_blank" style="color:var(--apx-accent);font-family:ui-monospace,monospace;font-size:11px;text-decoration:none;margin-left:8px">${esc(fqn)} ↗</a>`
            : `<span style="color:var(--apx-text);font-family:ui-monospace,monospace;font-size:11px;margin-left:8px">${esc(fqn)}</span>`;
          const sqlHeader = '<span style="color:var(--apx-muted);font-size:10px;text-transform:uppercase;letter-spacing:.05em">Unity Catalog Function</span>' + fnLink;
          html += `<div style="display:flex;align-items:center;padding:8px 0 4px">${sqlHeader}</div>`;
        }
      }
      if (Array.isArray(obj.data)) html += `<div style="color:var(--apx-muted);font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding:8px 0 4px">Results${obj._timing ? ' · ' + obj._timing : ''}</div>${fmtTable(obj.data)}`;
      return html;
    }
    if (tableShape(obj)) return renderToolOutput(rawStr);
  } catch {}
  try {
    const pretty = JSON.stringify(JSON.parse(rawStr), null, 2);
    return `<pre style="background:var(--apx-border);font-family:ui-monospace,monospace;font-size:11px;line-height:1.5;padding:10px 12px;border-radius:4px;overflow:auto;margin:0;color:var(--apx-muted)">${esc(pretty)}</pre>`;
  } catch {}
  return `<pre class="step-detail-pre">${esc(rawStr)}</pre>`;
}

// ── Inline thinking-steps ──
// Live tool-call rows rendered in the transcript above the answer bubble.
// Keyed by callId so the function_call (running) and its function_call_output
// (done/error) update the SAME row. The function_call_output item carries no
// `name`, so we stash the tool name on the row when it's created and reuse it.
const inlineSteps = {};  // callId -> row element (reset per send, see send handler)
function renderInlineStep(stepsContainer, callId, opts) {
  // opts: { name, phase: 'running'|'done'|'error', request, response }
  // The REQUEST (tool args — for a SQL tool, the query itself) and the
  // RESPONSE (rows) are kept as separate persistent sections so the query
  // is never overwritten by its result.
  let row = inlineSteps[callId];
  if (!row) {
    row = document.createElement('div');
    row.className = 'inline-step';
    row.innerHTML = '<div class="inline-step-head"></div><div class="inline-step-detail"></div>';
    row.querySelector('.inline-step-head').onclick = () => row.classList.toggle('open');
    stepsContainer.appendChild(row);
    inlineSteps[callId] = row;
  }
  if (opts.name) row.dataset.toolName = opts.name;
  if (opts.request != null) row._req = opts.request;
  if (opts.response != null) row._resp = opts.response;
  const name = opts.name || row.dataset.toolName || 'tool';
  const icon = opts.phase === 'running' ? '⚙' : (opts.phase === 'error' ? '✗' : '✓');
  const label = opts.phase === 'running' ? 'running…' : (opts.phase === 'error' ? 'error - details' : 'done - details');
  row.classList.toggle('error', opts.phase === 'error');
  if (document.body.classList.contains('apx-embed') && opts.phase !== 'running') {
    row.classList.add('open');
  }
  row.querySelector('.inline-step-head').innerHTML =
    `<span class="step-icon">${icon}</span><span class="step-name">${esc(name)}</span>`
    + `<span class="step-label">${label}</span><span class="step-caret">›</span>`;
  const detail = row.querySelector('.inline-step-detail');
  detail.innerHTML = '';
  // Show the request unless it's empty/no-arg ('{}'): a no-arg tool has no
  // query to show, so we skip the Request section rather than print '{}'.
  if (row._req != null && row._req !== '' && row._req.trim() !== '{}') {
    // For SQL tools: show the generated query as the request (not the raw args JSON)
    let reqBody = '';
    if (row._resp) {
      try {
        const rp = JSON.parse(row._resp);
        if (rp._sql) reqBody = fmtSql(rp._sql);
      } catch {}
    }
    if (!reqBody) reqBody = fmtReq(row._req);
    detail.insertAdjacentHTML('beforeend',
      `<div class="step-detail-label">Request</div>${reqBody}`);
  }
  if (row._resp != null) {
    detail.insertAdjacentHTML('beforeend',
      `<div class="step-detail-label">Response</div>${fmtResp(row._resp)}`);
  }
  chat.scrollTop = chat.scrollHeight;
}

// ── Tooltip ──
function showTip(e) {
  tooltip.textContent = e.target.closest('.tool-pill').dataset.tip;
  tooltip.classList.add('show');
  const r = e.target.getBoundingClientRect();
  tooltip.style.left = Math.min(r.left, window.innerWidth - 520) + 'px';
  tooltip.style.top = (r.bottom + 8) + 'px';
}
function hideTip() { tooltip.classList.remove('show'); }

// ── Input ──
inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
});
inputEl.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});

// ── Submit ──
form.addEventListener('submit', async e => {
  e.preventDefault();
  if (window.__APX_NATIVE__) return;
  const text = inputEl.value.trim();
  if (!text) return;
  document.getElementById('landing')?.remove();
  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;

  addMsg('user', text);
  addEvent('user', text.slice(0, 80), null, { content: text });
  history.push({ role: 'user', content: text });
  resetTrace();

  const assistantDiv = addMsg('assistant', '', true);
  // Live tool steps render into their own container ABOVE the answer bubble.
  for (const k in inlineSteps) delete inlineSteps[k];   // reset per send
  for (const k in toolGroups) delete toolGroups[k];
  for (const k in memCallBodies) delete memCallBodies[k];
  const stepsContainer = document.createElement('div');
  stepsContainer.className = 'inline-steps';
  chat.insertBefore(stepsContainer, assistantDiv);       // steps appear ABOVE the answer
  let full = '';
  let pendingTrace = null;
  let traceId = null;
  let traceStatus = 'completed';
  // Tool calls are surfaced live from the stream (below). When that happens we
  // tell finalizeTrace NOT to re-harvest them from the trace spans, so the
  // Events panel doesn't double up on workspaces where the trace also loads.
  let toolEventsFromStream = false;

  try {
    const res = await fetch('/invocations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-return-trace-id': 'true' },
      body: JSON.stringify({ input: history, stream: true, custom_inputs: { thread_id: devThreadId } }),
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    traceId = res.headers.get('x-apx-trace-id') || traceId;

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const payload = JSON.parse(line.slice(6));
          const ptype = payload.type || '';
          if (payload.trace_id && !traceId) traceId = payload.trace_id;
          if (ptype === 'response.output_text.delta' && payload.delta) {
            full += payload.delta;
            renderAssistantInto(assistantDiv, full);
            chat.scrollTop = chat.scrollHeight;
          } else if (ptype === 'response.output_item.done') {
            const item = payload.item || {};
            if (item.type === 'message' && Array.isArray(item.content)) {
              for (const part of item.content) {
                if (part.type === 'output_text' && part.text) {
                  full += part.text;
                  renderAssistantInto(assistantDiv, full);
                  chat.scrollTop = chat.scrollHeight;
                }
              }
            } else if (item.type === 'function_call') {
              // Surface the tool call (+ its SQL/args) live from the stream —
              // no trace fetch, so it works even when artifact-storage egress
              // is blocked. Detail pane shows the full arguments on click.
              toolEventsFromStream = true;
              const argStr = typeof item.arguments === 'string'
                ? item.arguments : JSON.stringify(item.arguments || {});
              // Group the call + its response by call_id in the Events panel.
              addToolCall(item.call_id || item.id || item.name, item.name,
                argStr.slice(0, 120), { arguments: argStr });
              // Also render the call live in the transcript as a step row.
              // Pass the args as `request` (for a SQL tool this IS the query),
              // kept separate from the result so it isn't overwritten on done.
              renderInlineStep(stepsContainer, item.call_id || item.id || item.name,
                { name: item.name, phase: 'running', request: argStr });
            } else if (item.type === 'function_call_output') {
              toolEventsFromStream = true;
              const outStr = typeof item.output === 'string'
                ? item.output : JSON.stringify(item.output || '');
              const isErr = /"error"|error/i.test(outStr);
              // Fill the response into the SAME group as its call (by call_id).
              addToolResponse(item.call_id || item.id || item.name, item.name,
                outStr.slice(0, 120), { output: outStr }, isErr);
              // Update the SAME step row (shared call_id) running → done/error.
              // Pass item.name unchanged (undefined on output items) so the row's
              // stashed tool name survives — a truthy fallback would overwrite it.
              renderInlineStep(stepsContainer, item.call_id || item.id || item.name,
                { name: item.name, phase: isErr ? 'error' : 'done', response: outStr });
            }
          } else if (ptype === 'response.completed' && !full) {
            const out = payload.response && payload.response.output;
            if (Array.isArray(out)) {
              for (const item of out) {
                if (item.type === 'message' && Array.isArray(item.content)) {
                  for (const part of item.content) {
                    if (part.type === 'output_text' && part.text) full += part.text;
                  }
                }
              }
              if (full) renderAssistantInto(assistantDiv, full);
            }
          } else if (ptype === 'tool.trace') {
            // Intentional dormant hook: no current producer emits ``tool.trace``.
            // Kept for a future server-side path that streams per-tool events so
            // inline pills can render mid-conversation. Today the Events panel
            // covers the same need by harvesting TOOL spans from the trace
            // after the response completes (see ``finalizeTrace``). Don't
            // delete in audit-chain sweeps.
            if (Array.isArray(payload.tools) && payload.tools.length) addToolPills(payload.tools);
          } else if (ptype === 'error') {
            traceStatus = 'error';
          }
        } catch {}
      }
    }
  } catch (err) {
    full = `Error: ${err.message}`;
    renderAssistantInto(assistantDiv, full);
    traceStatus = 'error';
  }

  assistantDiv.classList.remove('streaming');
  addEvent('assistant', full.slice(0, 80) + (full.length > 80 ? '…' : ''), null, { content: full });
  history.push({ role: 'assistant', content: full });
  traceId = await finalizeTrace(traceId, traceStatus, { emitEvents: !toolEventsFromStream });
  attachAssistantTrace(assistantDiv, traceId, traceStatus);
  sendBtn.disabled = false;
  inputEl.focus();
  // Refresh history list so the new/updated conversation appears.
  loadConversationHistory();
  // Surface any ASK-policy approval requests raised during this turn.
  checkPendingApprovals();
  // Auto-eval: check this response against any loaded eval criterion that
  // matches the question. Shows inline pass/fail badge on the assistant message.
  autoEvalResponse(text, full, traceId, assistantDiv);
});

// ── Resizable panel ──
const rightPanel = document.getElementById('right-panel');
const handle = document.getElementById('resize-handle');
let resizing = false;
handle.addEventListener('mousedown', () => { resizing = true; document.body.style.cursor = 'col-resize'; document.body.style.userSelect = 'none'; });
document.addEventListener('mousemove', e => {
  if (!resizing) return;
  const w = Math.max(280, Math.min(700, window.innerWidth - e.clientX));
  rightPanel.style.width = w + 'px';
});
document.addEventListener('mouseup', () => { resizing = false; document.body.style.cursor = ''; document.body.style.userSelect = ''; });

// Auto-populate the Trace panel on page load with the most recent run, so
// `reload → click Trace` shows context instead of an empty pane.
finalizeTrace(null, 'done');

inputEl.focus();
