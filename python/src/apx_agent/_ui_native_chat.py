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
</style>
<script>
function apxNativeChat({form, input, send, container, bubble, render}) {
  const key = '_apx_native_chat:' + location.pathname;
  let saved = JSON.parse(sessionStorage.getItem(key) || 'null');
  let session = saved ? saved.session : crypto.randomUUID();
  let busy = false;
  let restoring = false;
  let answer, status, eventLog, controls, cards;
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
    status = document.createElement('div');
    status.className = 'native-status';
    status.setAttribute('role', 'status');
    container.appendChild(status);
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
  function showEvent(raw) {
    const event = typeof raw === 'string' ? JSON.parse(raw) : raw;
    const row = document.createElement('pre');
    row.className = 'native-event';
    row.textContent = typeof raw === 'string' ? raw : JSON.stringify(event, null, 2);
    eventLog.appendChild(row);
    if (eventLog.querySelectorAll('pre').length > 64) eventLog.querySelector('pre').remove();
    if (!['agent.step.completed', 'agent.child.unknown'].includes(event.type)) return;
    const card = document.createElement('article');
    card.className = 'native-execution-card';
    const safe = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value) ? value : 'unavailable';
    // One readable line per step; the full provenance record stays under Details.
    const head = document.createElement('div');
    head.className = 'native-step-summary';
    card.appendChild(head);
    const record = document.createElement('details');
    const recordLabel = document.createElement('summary');
    recordLabel.textContent = 'Details';
    record.appendChild(recordLabel);
    card.appendChild(record);
    const line = (label, value) => {
      const row = document.createElement('div');
      row.textContent = label + ': ' + value;
      record.appendChild(row);
    };
    const link = (label, value, remoteUrl = null) => {
      const row = document.createElement('div');
      row.textContent = label + ': ';
      if (safe(value) === 'unavailable') row.append('unavailable');
      else {
        let href = '/api/invocations/' + encodeURIComponent(value);
        if (label === 'Child invocation') {
          href = null;
          try {
            const url = new URL(remoteUrl);
            if (typeof remoteUrl === 'string' && remoteUrl.length <= 2048 && ['http:', 'https:'].includes(url.protocol) &&
                !url.username && !url.password && !url.search && !url.hash &&
                url.pathname.endsWith('/api/invocations/' + encodeURIComponent(value))) href = url.href;
          } catch (_) {}
        }
        if (!href) { row.append(value + ' (record link unavailable)'); record.appendChild(row); return; }
        const a = document.createElement('a');
        a.href = href;
        a.textContent = value;
        row.appendChild(a);
      }
      record.appendChild(row);
    };
    const unknown = event.type === 'agent.child.unknown';
    const correlation = unknown ? event.correlation || {} : event;
    const transport = safe(event.transport_status), domain = safe(event.domain_status);
    head.textContent = [(transport === 'completed' && !unknown ? '✓ ' : '? ') + safe(event.step),
      ...(domain === 'unavailable' ? [] : [domain.replaceAll('_', ' ')]), unknown ? 'remote' : safe(event.location)].join(' · ');
    line('Step', safe(event.step));
    line('Location', unknown ? 'remote' : safe(event.location));
    line('Transport', safe(event.transport_status));
    line('Domain', safe(event.domain_status));
    link('Invocation', event.invocation_id);
    link('Child invocation', unknown ? event.id : event.child_invocation_id, event.child_invocation_url);
    link('Parent invocation', correlation.parent_invocation_id);
    line('Sender', safe(correlation.transport_sender));
    line('Predecessor', safe(correlation.logical_predecessor));
    line('Schema validated', event.schema_validated === true ? 'true' : 'unavailable');
    line('Readback verified', event.readback_verified === true ? 'true' : 'unavailable');
    for (const direction of ['input', 'output']) {
      line(direction + ' schema', safe(event[direction + '_schema']));
      const artifact = event[direction + '_artifact'] || {};
      line(direction + ' artifact', safe(artifact.artifact_id));
      line(direction + ' SHA256', typeof artifact.sha256 === 'string' && /^[a-f0-9]{64}$/.test(artifact.sha256) ? artifact.sha256 : 'unavailable');
    }
    const timestamp = typeof event.recorded_at === 'string' && event.recorded_at.length <= 64 &&
      /^\d{4}-\d\d-\d\dT[\d:.+-]+Z?$/.test(event.recorded_at) && event.timestamp_source === 'application_emit';
    line('Server event time', timestamp ? event.recorded_at + ' (application_emit)' : 'unavailable');
    line('Child timing', 'unavailable');
    line('Trace', 'unavailable');
    line('Model/version', 'unavailable');
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
        container.insertBefore(bubble('user', requestText()), status);
        saved.events.forEach(showEvent);
        restoring = false;
        controls.querySelectorAll('button').forEach(button => button.disabled = false);
      }
      await events(path);
      const recorded = await (await checked(path)).text();
      const state = JSON.parse(recorded);
      saved.terminal = state.status === 'completed' || state.status === 'failed';
      if (state.status === 'failed') {
        status.textContent = 'Failed: ' + (typeof state.error === 'string' ? state.error : 'Invocation failed');
      } else if (state.status === 'completed' && state.output?.status === 'interrupted') {
        status.textContent = 'Interrupted: approval required' + (state.output.approval_required?.tool_name ? ' for ' + state.output.approval_required.tool_name : '');
      } else if (state.status === 'completed' && state.output?.status === 'completed') {
        const messages = state.output.messages || [];
        // A sequence's declared answer (a follow-up question or a summary) is the whole answer.
        const halt = messages.filter(m => m.type === 'ai' && m.additional_kwargs?.apx_answer === true);
        const text = (halt.length ? halt : messages.filter(m => m.type === 'ai')).map(m =>
          typeof m.content === 'string' ? m.content : (m.content || []).filter(p => p.type === 'text').map(p => p.text).join('')
        ).filter(Boolean).join('\n');
        if (!answer) answer = bubble('assistant', '');
        render(answer, text || '(empty response)');
        const replies = halt.map(m => m.additional_kwargs.apx_replies).find(r => Array.isArray(r) && r.length);
        if (replies && !schema) {
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
        if (messages.some(m => m.type === 'ai' && m.additional_kwargs?.apx_data_parts?.length)) {
          const details = document.createElement('details');
          details.className = 'native-recorded-output';
          const summary = document.createElement('summary');
          summary.textContent = 'Recorded invocation response (exact JSON)';
          const output = document.createElement('pre');
          output.className = 'native-output';
          output.textContent = recorded.length <= 131072 ? recorded : 'Recorded response exceeds display limit; retrieve the invocation record.';
          details.append(summary, output);
          answer.appendChild(details);
        }
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
