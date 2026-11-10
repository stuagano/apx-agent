"""Shared native invocation client for the two existing chat renderers."""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError


def native_input_router(agent: Any) -> APIRouter:
    """Discover and validate the server declaration without executing a workflow."""
    from ._native_input import input_leaf, native_data_part

    schema = getattr(input_leaf(agent), "_input_schema", None)
    router = APIRouter()

    @router.get("/api/input-schema", include_in_schema=False)
    async def input_schema() -> Any:
        return schema.model_json_schema() if schema is not None else None

    @router.post("/api/input-validation", include_in_schema=False)
    async def validate_input(request: Request) -> Any:
        if schema is None:
            raise HTTPException(422, "This agent accepts text input")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Declared input exceeds 64 KiB")
        try:
            schema.model_validate_json(body, strict=True)
            return native_data_part(schema, json.loads(body))
        except ValidationError as exc:
            # Locations and messages help correction without reflecting submitted values.
            detail = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors(include_input=False)[:16])
            raise HTTPException(422, detail[:2048]) from None
        except (ValueError, TypeError):
            raise HTTPException(422, "Provide a JSON object matching the declared schema") from None

    return router

NATIVE_CHAT_SCRIPT = r"""
<style>
.native-execution-card {border: 1px solid var(--apx-border); border-radius: 8px; padding: 12px; margin: 8px 0; overflow-wrap: anywhere;}
.native-event, .native-output, .native-input-schema pre {white-space: pre-wrap; overflow-wrap: anywhere; max-height: 320px; overflow: auto;}
.native-validation {color: var(--apx-err);}
.native-status-row {display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin: 12px 0;}
.native-result-button {border: 1px solid var(--apx-border); border-radius: 6px; padding: 7px 14px; font: inherit; font-weight: 600; cursor: pointer;}
.native-result-button:disabled {opacity: .5; cursor: default;}
.native-result-panel {border: 1px solid var(--apx-border); border-radius: 8px; padding: 16px; margin-bottom: 12px; overflow-wrap: anywhere; min-width: 0;}
.native-result-panel h3 {margin: 0 0 12px;}
.native-result-panel h4 {margin: 16px 0 8px;}
.native-result-panel .native-tree-table {display: block; max-width: 100%; overflow-x: auto;}
.native-result-answer {white-space: pre-wrap;}
.native-result-packet {margin: 12px 0;}
@media (max-width: 700px) {
  .main:has(.native-status-row) {flex-direction: column; overflow-y: auto;}
  .main:has(.native-status-row) .chat-panel {flex: 1 0 70vh;}
  .main:has(.native-status-row) .right-panel {width: 100%; min-width: 0; max-width: none; flex: 0 0 40vh;}
}
</style>
<script>
// #978: a reload's earlier turns from the session checkpoint, as {role, text}. The saved run
// restores its own turn, so the transcript stops before its last matching user message.
function apxPriorTurns(messages, current) {
  const text = m => m.type === 'human' && m.additional_kwargs?.apx_data_parts
    ? JSON.stringify(m.additional_kwargs.apx_data_parts[0].data, null, 2)
    : typeof m.content === 'string' ? m.content
    : (Array.isArray(m.content) ? m.content : []).filter(p => p?.type === 'text').map(p => p.text).join('');
  const turns = messages.filter(m => m.type === 'human' || m.type === 'ai')
    .map(m => ({role: m.type === 'human' ? 'user' : 'assistant', text: text(m)})).filter(t => t.text);
  const last = turns.map(t => t.role === 'user' && t.text === current).lastIndexOf(true);
  return last >= 0 ? turns.slice(0, last) : turns;
}
function apxNativeChat({form, input, send, container, bubble, render}) {
  const key = '_apx_native_chat:' + location.pathname;
  let saved = JSON.parse(sessionStorage.getItem(key) || 'null');
  let session = saved ? saved.session : crypto.randomUUID();
  let busy = false;
  let restoring = false;
  let answered = false; // the Events timeline already has this run's assistant row
  let answer, status, eventLog, controls, cards, resultButton, resultPanel;
  let schema, ready = false;
  const validation = document.createElement('div');
  validation.className = 'native-validation';
  validation.setAttribute('role', 'alert');
  form.before(validation);
  send.disabled = true;
  checked('/api/input-schema').then(response => response.json()).then(value => {
    schema = value;
    if (schema) {
      const details = document.createElement('details');
      details.className = 'native-input-schema';
      const title = document.createElement('summary');
      title.textContent = 'Required request JSON — declared input schema';
      const pre = document.createElement('pre');
      pre.textContent = JSON.stringify(schema, null, 2);
      details.append(title, pre);
      form.before(details);
      input.placeholder = 'Enter a JSON object matching the declared schema';
      input.setAttribute('aria-label', 'Declared request JSON');
    }
    ready = true;
    send.disabled = busy || Boolean(saved && !saved.terminal);
  }).catch(() => { validation.textContent = 'Input declaration unavailable. Reload to check before submitting.'; });
  const persist = () => sessionStorage.setItem(key, JSON.stringify(saved));
  const requestText = () => saved.userText ?? (saved.request.input.apx_data_parts
    ? JSON.stringify(saved.request.input.apx_data_parts[0].data, null, 2) : saved.request.input.messages[0].content);
  function prepare() {
    document.getElementById('landing')?.remove();
    if (!restoring) bubble('user', requestText());
    answered = false;
    if (!restoring && typeof addEvent === 'function') addEvent('user', 'user', requestText().slice(0, 200));
    status = document.createElement('div');
    status.className = 'native-status';
    status.setAttribute('role', 'status');
    const statusRow = document.createElement('div');
    statusRow.className = 'native-status-row';
    resultButton = document.createElement('button');
    resultButton.type = 'button';
    resultButton.className = 'native-result-button';
    resultButton.textContent = 'View result';
    resultButton.disabled = true;
    resultButton.setAttribute('aria-expanded', 'false');
    resultPanel = document.createElement('section');
    resultPanel.className = 'native-result-panel';
    resultPanel.id = 'native-result-' + saved.request.id;
    resultPanel.setAttribute('role', 'region');
    resultPanel.setAttribute('aria-label', 'Run result');
    resultPanel.hidden = true;
    resultButton.setAttribute('aria-controls', resultPanel.id);
    // Capture this run's nodes: earlier result buttons must never open the newest run.
    const panel = resultPanel, button = resultButton;
    button.onclick = () => {
      panel.hidden = !panel.hidden;
      button.setAttribute('aria-expanded', String(!panel.hidden));
    };
    statusRow.append(status, resultButton);
    container.append(statusRow, resultPanel);
    eventLog = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'Run events';
    eventLog.appendChild(summary);
    container.appendChild(eventLog);
    cards = document.createElement('div');
    cards.className = 'native-execution-cards';
    container.appendChild(cards);
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
  // A typed output, one level at a time: plain values show; each nested object or list is a
  // closed row with a count, and a list of flat records opens as a small table. Text only, bounded.
  const leaf = v => v === null ? 'null' : typeof v === 'string' ? (v.length > 300 ? v.slice(0, 299) + '…' : v) : String(v);
  const isFlat = v => v !== null && typeof v === 'object' && !Array.isArray(v) &&
    Object.values(v).every(x => x === null || typeof x !== 'object');
  function table(rows) {
    const cols = [...new Set(rows.flatMap(r => Object.keys(r)))].slice(0, 8);
    const t = document.createElement('table');
    t.className = 'native-tree-table';
    const hr = t.createTHead().insertRow();
    cols.forEach(c => { const th = document.createElement('th'); th.textContent = c; hr.appendChild(th); });
    const body = t.createTBody();
    rows.slice(0, 50).forEach(r => { const tr = body.insertRow(); cols.forEach(c => { tr.insertCell().textContent = Object.hasOwn(r, c) ? leaf(r[c]) : ''; }); });
    return t;
  }
  function tree(value, depth = 0) {
    const node = document.createElement('div');
    node.className = 'native-tree';
    if (depth > 0) node.style.paddingLeft = '1em';
    if (value === null || typeof value !== 'object') { node.textContent = leaf(value); return node; }
    if (depth >= 6) { node.textContent = '…'; return node; }
    if (Array.isArray(value) && value.length && value.every(isFlat)) {
      node.appendChild(table(value));
      if (value.length > 50) { const more = document.createElement('div'); more.textContent = '… ' + (value.length - 50) + ' more'; node.appendChild(more); }
      return node;
    }
    const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v]) : Object.entries(value);
    entries.slice(0, 50).forEach(([key, item]) => {
      if (item !== null && typeof item === 'object') {
        const sub = document.createElement('details');
        const label = document.createElement('summary');
        const n = Array.isArray(item) ? item.length : Object.keys(item).length;
        label.textContent = key.slice(0, 80) + ' ' + n + (Array.isArray(item) ? (n === 1 ? ' item' : ' items') : (n === 1 ? ' field' : ' fields'));
        sub.appendChild(label);
        // Built on first open, so a large output costs nothing until someone looks.
        sub.addEventListener('toggle', () => { if (sub.open && sub.children.length === 1) sub.appendChild(tree(item, depth + 1)); });
        node.appendChild(sub);
      } else {
        const row = document.createElement('div');
        row.textContent = key.slice(0, 80) + ' ' + leaf(item);
        node.appendChild(row);
      }
    });
    if (entries.length > 50) {
      const more = document.createElement('div');
      more.textContent = '… ' + (entries.length - 50) + ' more';
      node.appendChild(more);
    }
    return node;
  }
  const messageText = m => typeof m.content === 'string' ? m.content :
    (Array.isArray(m.content) ? m.content : []).filter(p => p?.type === 'text').map(p => p.text).join('');
  function showResult(state, recorded, selected, messages) {
    const text = selected.map(m => {
      const content = messageText(m);
      // Typed runtimes also put canonical JSON in each message's content.
      // Hide only exact packet duplicates; preserve prose and JSON-only fallbacks.
      try {
        const value = JSON.stringify(JSON.parse(content));
        if ((m.additional_kwargs?.apx_data_parts || []).some(p => p?.kind === 'data' && JSON.stringify(p.data) === value)) return '';
      } catch (_) { /* The recorded answer is ordinary text. */ }
      return content;
    }).filter(Boolean).join('\n');
    resultPanel.replaceChildren();
    const parts = messages.filter(m => m.type === 'ai').flatMap(m => m.additional_kwargs?.apx_data_parts || [])
      .filter(p => p?.kind === 'data' && p.data !== null && typeof p.data === 'object' && Object.keys(p.data).length);
    const approval = state.output?.status === 'interrupted' && state.output?.approval_required;
    const available = state.status === 'completed' && (state.output?.status === 'completed' || approval) &&
      Boolean(text.trim() || parts.length || approval);
    resultButton.disabled = !available;
    if (!available) {
      resultPanel.hidden = true;
      resultButton.setAttribute('aria-expanded', 'false');
      return;
    }
    const heading = document.createElement('h3');
    heading.textContent = 'Run result';
    resultPanel.appendChild(heading);
    if (text.trim()) {
      const answerText = document.createElement('div');
      answerText.className = 'native-result-answer';
      answerText.textContent = text;
      resultPanel.appendChild(answerText);
    }
    const approvalStatus = document.createElement('p');
    approvalStatus.className = 'native-result-approval';
    approvalStatus.textContent = approval ? 'Approval required — agent execution paused' : 'Approval status not recorded';
    resultPanel.appendChild(approvalStatus);
    if (approval) resultPanel.appendChild(tree(approval));
    if (parts.length) {
      const label = document.createElement('h4');
      label.textContent = 'Returned packets and evidence';
      resultPanel.appendChild(label);
      // A declared answer can carry a copy of a preceding step's packet.
      const seen = new Set();
      parts.forEach(part => {
        const identity = JSON.stringify(part.data);
        if (seen.has(identity)) return;
        seen.add(identity);
        if (seen.size > 50) return;
        const packet = document.createElement('div');
        packet.className = 'native-result-packet';
        packet.appendChild(tree(part.data));
        resultPanel.appendChild(packet);
      });
      if (seen.size > 50) resultPanel.append('Additional packets omitted from preview; see exact JSON.');
    }
    const details = document.createElement('details');
    details.className = 'native-recorded-output';
    const summary = document.createElement('summary');
    summary.textContent = 'Recorded invocation response (exact JSON)';
    const output = document.createElement('pre');
    output.className = 'native-output';
    output.textContent = recorded.length <= 131072 ? recorded : 'Recorded response exceeds display limit; retrieve the invocation record.';
    details.append(summary, output);
    resultPanel.appendChild(details);
  }
  // Data parts by sha: how a step card finds the payloads its event names by digest.
  const indexParts = (parts, bySha) => parts.forEach(part => {
    const sha = part?.metadata?.sha256;
    if (typeof sha === 'string') bySha[sha] = part.data;
  });
  const disclosure = (className, label, value) => {
    const view = document.createElement('details');
    view.className = className;
    const summary = document.createElement('summary');
    summary.textContent = label;
    view.append(summary, tree(value));
    return view;
  };
  function attachOutputs(messages) {
    const bySha = {};
    messages.forEach(m => indexParts(m.additional_kwargs?.apx_data_parts || [], bySha));
    cards.querySelectorAll('.native-execution-card[data-sha]').forEach(card => {
      if (!(card.dataset.sha in bySha) || card.querySelector('.native-step-output')) return;
      card.insertBefore(disclosure('native-step-output', 'Output · ' + card.dataset.schema, bySha[card.dataset.sha]),
        card.querySelector('.native-step-record'));
    });
    attachInputs(bySha);
  }
  // A step's input is the request this run sent or an earlier step's output; both are on the page (#1123).
  function attachInputs(bySha) {
    let sent = [];
    // The sent input lives only in the exact request wire; parse a copy for display.
    try { sent = JSON.parse(saved?.requestWire || '{}').input?.apx_data_parts || []; } catch (_) { /* Not a typed request. */ }
    indexParts(sent, bySha);
    cards.querySelectorAll('.native-execution-card[data-input-sha]').forEach(card => {
      if (card.querySelector('.native-step-input')) return;
      const sha = card.dataset.inputSha, label = 'Input · ' + card.dataset.inputSchema;
      let view;
      if (sha in bySha) view = disclosure('native-step-input', label, bySha[sha]);
      else {
        // Only the digest is on this page (e.g. an input rebuilt by input_preparation); say no more than that.
        view = document.createElement('div');
        view.className = 'native-step-input';
        const digest = document.createElement('span');
        digest.textContent = sha.slice(0, 8) + '…';
        digest.title = sha;
        view.append(label + ' — not on this page ', digest);
      }
      card.insertBefore(view, card.querySelector('.native-step-output') || card.querySelector('.native-step-record'));
    });
  }
  // The Events tab timeline, fed from the durable stream (rows are textContent-only; see addEvent).
  function timeline(event) {
    if (typeof addEvent !== 'function') return;
    const text = c => typeof c === 'string' ? c : Array.isArray(c) ? c.filter(p => p?.type === 'text').map(p => p.text).join('') : '';
    const m = event.message || {};
    if (event.type === 'agent.message.delta' && m.type === 'ai') {
      (m.tool_calls || []).forEach(call => addEvent('tool-call', String(call?.name || 'tool'), JSON.stringify(call?.args ?? {}).slice(0, 200), call));
      const said = text(m.content);
      if (said) { answered = true; addEvent('assistant', 'assistant', said.slice(0, 200), {content: said}); }
    } else if (event.type === 'agent.message.delta' && m.type === 'tool') {
      addEvent(m.status === 'error' ? 'tool-error' : 'tool-result', String(m.name || 'tool'), text(m.content).slice(0, 200), m);
    } else if (event.type === 'agent.step.completed' || event.type === 'agent.child.unknown') {
      addEvent('step', 'step · ' + String(event.step || 'remote'), event.domain_status ? String(event.domain_status) : '', event);
    }
  }
  function showEvent(raw) {
    const event = typeof raw === 'string' ? JSON.parse(raw) : raw;
    const row = document.createElement('pre');
    row.className = 'native-event';
    row.textContent = typeof raw === 'string' ? raw : JSON.stringify(event, null, 2);
    eventLog.appendChild(row);
    if (eventLog.querySelectorAll('pre').length > 64) eventLog.querySelector('pre').remove();
    timeline(event);
    if (!['agent.step.completed', 'agent.child.unknown'].includes(event.type)) return;
    const card = document.createElement('article');
    card.className = 'native-execution-card';
    const safe = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value) ? value : 'unavailable';
    // One readable line per step; the full provenance record stays under Details.
    const head = document.createElement('div');
    head.className = 'native-step-summary';
    card.appendChild(head);
    // The step's typed output is attached once the run's recorded parts arrive (matched by sha).
    const sha = event.output_artifact?.sha256;
    if (typeof sha === 'string' && /^[a-f0-9]{64}$/.test(sha)) card.dataset.sha = sha;
    card.dataset.schema = safe(event.output_schema);
    // A declared input (schema + digest) is resolved the same way, from the sent request or an earlier output.
    const inputSha = event.input_artifact?.sha256;
    if (typeof inputSha === 'string' && /^[a-f0-9]{64}$/.test(inputSha) && safe(event.input_schema) !== 'unavailable') {
      card.dataset.inputSha = inputSha;
      card.dataset.inputSchema = safe(event.input_schema);
    }
    const record = document.createElement('details');
    record.className = 'native-step-record';
    const recordLabel = document.createElement('summary');
    recordLabel.textContent = 'Details';
    record.appendChild(recordLabel);
    card.appendChild(record);
    const known = v => safe(v) !== 'unavailable';
    const missing = [];
    // Short ids and hashes read better; the full value stays on hover (title) and in copies.
    const short = (text, full) => {
      const span = document.createElement('span');
      span.textContent = text.length > 12 ? text.slice(0, 8) + '…' : text;
      span.title = full || text;
      return span;
    };
    const addRow = (...parts) => {
      const div = document.createElement('div');
      parts.forEach(part => div.append(part));
      record.appendChild(div);
    };
    const unknown = event.type === 'agent.child.unknown';
    const correlation = unknown ? event.correlation || {} : event;
    const transport = safe(event.transport_status), domain = safe(event.domain_status);
    // A remote step names the App it ran in (from its verified child record link); a local one says nothing.
    const childId = unknown ? event.id : event.child_invocation_id;
    let childHref = null, childApp = null, childHost = null;
    try {
      const url = new URL(event.child_invocation_url);
      if (typeof event.child_invocation_url === 'string' && event.child_invocation_url.length <= 2048 &&
          ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash &&
          known(childId) && url.pathname.endsWith('/api/invocations/' + encodeURIComponent(childId))) {
        childHref = url.href;
        childHost = url.hostname.slice(0, 253);
        childApp = childHost.split('.')[0].replace(/-\d+$/, '').slice(0, 40);
      }
    } catch (_) {}
    const remote = unknown || event.location === 'remote';
    head.textContent = [(transport === 'completed' && !unknown ? '✓ ' : '? ') + safe(event.step),
      ...(domain === 'unavailable' ? [] : [domain.replaceAll('_', ' ')]),
      ...(remote ? [childApp ? '→ ' + childApp : 'remote'] : [])].join(' · ');
    // The label is derived from the event's URL, so the full host always travels with it.
    if (childHost) head.title = 'ran in ' + childHost;
    if (!known(event.step)) missing.push('step');
    if (transport !== 'completed') addRow('transport ' + transport);
    const timestamp = typeof event.recorded_at === 'string' && event.recorded_at.length <= 64 &&
      /^\d{4}-\d\d-\d\dT[\d:.+-]+Z?$/.test(event.recorded_at) && event.timestamp_source === 'application_emit';
    const head1 = [];
    if (known(event.invocation_id)) head1.push(['invocation ', event.invocation_id]);
    if (known(correlation.transport_sender)) head1.push(['sender ', correlation.transport_sender]);
    if (head1.length || timestamp) {
      const div = document.createElement('div');
      head1.forEach(([label, value], i) => {
        if (i) div.append(' · ');
        div.append(label);
        if (label === 'invocation ') {
          const a = document.createElement('a');
          a.href = '/api/invocations/' + encodeURIComponent(value);
          a.append(short(value));
          div.append(a);
        } else div.append(value);
      });
      if (timestamp) div.append((head1.length ? ' · ' : '') + event.recorded_at.slice(0, 19).replace('T', ' ') + ' UTC');
      record.appendChild(div);
    } else missing.push('invocation');
    if (!timestamp) missing.push('event time');
    if (known(childId)) {
      if (childHref) {
        const a = document.createElement('a');
        a.href = childHref;
        a.textContent = childId;
        addRow('child ', a);
        addRow('ran in ' + childHost);
      } else addRow('child ' + childId + ' (record link unavailable)');
    } else if (remote) missing.push('child invocation');
    if (known(correlation.parent_invocation_id)) addRow('parent ', short(correlation.parent_invocation_id));
    if (known(correlation.logical_predecessor)) addRow('predecessor ' + correlation.logical_predecessor);
    for (const direction of ['input', 'output']) {
      const schema = safe(event[direction + '_schema']);
      const artifact = event[direction + '_artifact'] || {};
      const digest = typeof artifact.sha256 === 'string' && /^[a-f0-9]{64}$/.test(artifact.sha256) ? artifact.sha256 : null;
      if (schema === 'unavailable' && !digest) { if (direction === 'output') missing.push('output'); continue; }
      addRow(direction + ' ' + (schema === 'unavailable' ? '' : schema + ' '), ...(digest ? [short(digest)] : []));
    }
    if (event.schema_validated === true) addRow('schema validated ✓');
    if (event.readback_verified === true) addRow('readback verified ✓');
    missing.push('child timing', 'trace', 'model/version');
    addRow('not recorded: ' + missing.join(', '));
    cards.appendChild(card);
    if (cards.children.length > 64) cards.firstChild.remove();
  }
  async function checked(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      const error = new Error('HTTP ' + response.status);
      error.status = response.status;
      throw error;
    }
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
          const event = data.length <= 8192 ? data : JSON.stringify({type: 'event.omitted', detail: 'Event exceeds display limit'}, null, 2);
          saved.events.push(event);
          saved.events = saved.events.slice(-64);
          saved.cursor = id;
          persist();
          showEvent(event);
        }
        if (buffer.length > 131072) throw new Error('Event exceeds stream limit');
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
      if (post) {
        let submit = !saved.attempted;
        if (saved.attempted) {
          try { await checked(path); }
          catch (error) { if (error.status === 404) submit = true; else throw error; }
        }
        if (submit) {
          saved.attempted = true;
          persist();
          await checked('/api/invocations', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: saved.requestWire || JSON.stringify(saved.request),
          });
        }
      }
      else await checked(path); // Reconnect reads existing state; it never submits.
      // Check access before displaying cached content after a reload/account switch.
      if (restoring) {
        container.insertBefore(bubble('user', requestText()), status.parentElement);
        if (typeof addEvent === 'function') addEvent('user', 'user', requestText().slice(0, 200));
        saved.events.forEach(showEvent);
        restoring = false;
        controls.querySelectorAll('button').forEach(button => button.disabled = false);
      }
      await events(path);
      const recorded = await (await checked(path)).text();
      const state = JSON.parse(recorded);
      saved.terminal = state.status === 'completed' || state.status === 'failed';
      const messages = Array.isArray(state.output?.messages) ? state.output.messages : [];
      // Only explicitly marked answers override the ordinary assistant text fallback.
      const halt = messages.filter(m => m.type === 'ai' && m.additional_kwargs?.apx_answer === true);
      const selected = halt.length ? halt : messages.filter(m => m.type === 'ai');
      const text = selected.map(messageText).filter(Boolean).join('\n');
      showResult(state, recorded, selected, messages);
      if (state.status === 'failed') {
        status.textContent = 'Failed: ' + (typeof state.error === 'string' ? state.error : 'Invocation failed');
      } else if (state.status === 'completed' && state.output?.status === 'interrupted') {
        status.textContent = 'Interrupted: approval required' + (state.output.approval_required?.tool_name ? ' for ' + state.output.approval_required.tool_name : '');
      } else if (state.status === 'completed' && state.output?.status === 'completed') {
        if (text.trim()) {
          if (!answer) answer = bubble('assistant', '');
          render(answer, text);
        }
        // A sequence streams only step completions, so its answer closes the timeline here.
        if (text && !answered && typeof addEvent === 'function') { answered = true; addEvent('assistant', 'assistant', text.slice(0, 200), {content: text}); }
        const replies = halt.map(m => m.additional_kwargs.apx_replies).find(r => Array.isArray(r) && r.length);
        if (replies && !schema) {
          if (!answer) answer = bubble('assistant', '');
          const row = document.createElement('div');
          row.className = 'native-quick-replies';
          replies.filter(r => typeof r === 'string').forEach(reply => {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = reply;
            button.addEventListener('click', () => {
              if (busy || !ready || (saved && !saved.terminal)) return;
              input.value = reply;
              form.requestSubmit();
            });
            row.appendChild(button);
          });
          answer.appendChild(row);
        }
        attachOutputs(messages);
        status.textContent = resultButton.disabled ? 'Completed — no recorded result' : 'Completed';
      } else {
        status.textContent = state.status === 'completed' ? 'Completed — result unavailable. Reconnect to check this run.' : 'Result unavailable. Reconnect to check this run.';
        controls.hidden = false;
      }
      persist();
    } catch (error) {
      status.textContent = 'Connection uncertain (' + error.message + '). Reconnect to check this run'
        + (restoring ? '.' : ', or retry the same request.');
      controls.hidden = false;
    } finally {
      busy = false;
      send.disabled = !ready || !saved.terminal;
    }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    event.stopImmediatePropagation();
    const text = schema ? input.value : input.value.trim();
    if (!text.trim() || !ready || busy || (saved && !saved.terminal)) return;
    validation.textContent = '';
    // An earlier answer's quick replies must not act on whatever is pending now.
    document.querySelectorAll('.native-quick-replies button').forEach(button => { button.disabled = true; });
    let inputWire;
    if (schema) {
      busy = true;
      send.disabled = true;
      try {
        JSON.parse(text);
        const response = await fetch('/api/input-validation', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: text});
        const partWire = await response.text();
        if (!response.ok) {
          const body = JSON.parse(partWire);
          throw new Error(typeof body.detail === 'string' ? body.detail : 'Check the declared input schema');
        }
        inputWire = '{"messages":[],"apx_data_parts":[' + partWire + ']}';
      } catch (error) {
        validation.textContent = 'Request needs correction: ' + error.message;
        busy = false;
        send.disabled = false;
        return;
      }
      busy = false;
    }
    const request = {id: crypto.randomUUID(), session_id: session, background: true, stream: true};
    inputWire = inputWire || JSON.stringify({messages: [{role: 'user', content: text}]});
    // Preserve the server's numeric JSON beside its digest, including in saved retries.
    const requestWire = JSON.stringify(request).slice(0, -1) + ',"input":' + inputWire + '}';
    saved = {session, request, requestWire, userText: text,
      cursor: 0, events: [], terminal: false};
    restoring = false;
    persist();
    input.value = '';
    prepare();
    follow(true);
  }, true);
  async function priorTurns() {
    try {
      const response = await fetch('/api/session/history?session_id=' + encodeURIComponent(session));
      if (!response.ok) return;
      const turns = apxPriorTurns((await response.json()).messages || [], requestText());
      if (turns.length) document.getElementById('landing')?.remove();
      turns.forEach(t => t.role === 'user' ? bubble('user', t.text) : render(bubble('assistant', ''), t.text));
    } catch (_) { /* Earlier turns are best-effort; the saved run below still restores. */ }
  }
  if (saved) { restoring = true; priorTurns().then(() => { prepare(); follow(false); }); }
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
