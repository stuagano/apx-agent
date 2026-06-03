# Inline Thinking-Steps (live tool steps in the chat transcript) — Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. TDD. Steps use `- [ ]`.

**Goal:** Show the agent's actions live in the chat transcript as it works (Genie-Code style): each tool call appears as an expandable step row — `⚙ run_sql · running…` → `✓ run_sql · done` (click to expand the SQL/args + result) — positioned above the streaming answer. So the user sees what's happening *during* the response, not only in the Events panel afterward.

**Design (approved):** Expandable step rows; **action-steps only** (tool calls + results; no raw model reasoning). Purely additive frontend in `_ui_chat.py`: the stream handler already parses `function_call` / `function_call_output` items (#129, routed to the Events panel) — now ALSO render them as inline transcript step rows, keyed by `call_id`, inserted **before** the answer bubble. Events panel behavior is unchanged (both fed from the same stream items).

**Tech Stack:** the chat send-handler JS in `_ui_chat.py` (f-string — double literal braces). No backend/stream change.

**Out of scope:** raw model reasoning/CoT tokens; changing the stream wire format; the Events panel (#129 stays as-is); the Setup/tool-gen "No tables" issue.

**Files:** Modify `python/src/apx_agent/_ui_chat.py`; Test `python/tests/test_dev_ui_routes.py`.

---

### Task 1: Inline step renderer + CSS

- [ ] **Step 1 — failing test** (`tests/test_dev_ui_routes.py`, reuse the existing ctx-with-a-tool pattern from `TestEventsToolCalls`):
```python
class TestInlineSteps:
    def test_stream_renders_inline_tool_steps(self):
        from apx_agent._ui_chat import _render_agent_ui
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._models import AgentTool
        cfg = AgentConfig(name="d", description="x", examples=[])
        ctx = AgentContext(
            config=cfg,
            tools=[AgentTool(name="run_sql", description="Run SQL",
                             input_schema={"type": "object", "properties": {}})],
            card={"name": "d", "skills": []}, agent=None,  # type: ignore[arg-type]
        )
        html = _render_agent_ui(ctx)
        assert "function renderInlineStep" in html          # the renderer exists
        assert "stepsContainer" in html                      # transcript container
        assert "renderInlineStep(" in html                   # called from the stream branches
        assert "insertBefore(stepsContainer" in html         # steps sit above the answer bubble
        assert ".inline-step" in html                        # styling present
```
- [ ] **Step 2 — run, fails.**

- [ ] **Step 3 — add the renderer** in `_ui_chat.py` near `addToolPills` (~line 1395). It create-or-updates a step row keyed by `callId`, tracked in a per-turn map:
```javascript
const inlineSteps = {{}};  // callId -> row element (reset per send, see Task 2)
function renderInlineStep(stepsContainer, callId, opts) {{
  // opts: {{ name, phase: 'running'|'done'|'error', detail }}
  let row = inlineSteps[callId];
  if (!row) {{
    row = document.createElement('div');
    row.className = 'inline-step';
    row.innerHTML = '<div class="inline-step-head"></div><pre class="inline-step-detail"></pre>';
    row.querySelector('.inline-step-head').onclick = () => row.classList.toggle('open');
    stepsContainer.appendChild(row);
    inlineSteps[callId] = row;
  }}
  const icon = opts.phase === 'running' ? '⚙' : (opts.phase === 'error' ? '✗' : '✓');
  const label = opts.phase === 'running' ? 'running…' : (opts.phase === 'error' ? 'error' : 'done');
  row.classList.toggle('error', opts.phase === 'error');
  row.querySelector('.inline-step-head').innerHTML =
    `<span class="step-icon">${{icon}}</span><span class="step-name">${{esc(opts.name || 'tool')}}</span>`
    + `<span class="step-label">${{label}}</span>`;
  if (opts.detail != null) row.querySelector('.inline-step-detail').textContent = opts.detail;
  chat.scrollTop = chat.scrollHeight;
}}
```
  (Use the file's existing HTML-escape helper — it's `esc(...)` per other call sites; confirm the name. `chat` and `addToolPills`'s neighbours are in scope here.)

- [ ] **Step 4 — add CSS** in the `<style>` block (double the braces):
```css
  .inline-step {{ background: #0e1116; border: 1px solid #1f242b; border-radius: 8px; margin: 6px 0; padding: 0; max-width: 680px; }}
  .inline-step.error {{ border-color: #3a1a1a; }}
  .inline-step-head {{ display: flex; align-items: center; gap: 8px; padding: 8px 12px; cursor: pointer; font-size: 12.5px; }}
  .inline-step-head .step-icon {{ color: #60b0ff; }}
  .inline-step.error .step-icon {{ color: #f87171; }}
  .inline-step-head .step-name {{ color: #cfe; font-family: ui-monospace, monospace; }}
  .inline-step-head .step-label {{ color: #6b7280; margin-left: auto; font-size: 11px; }}
  .inline-step-detail {{ display: none; margin: 0; padding: 0 12px 10px; color: #8a929b; font-size: 11px;
                         white-space: pre-wrap; font-family: ui-monospace, monospace; }}
  .inline-step.open .inline-step-detail {{ display: block; }}
```
- [ ] **Step 5 — run test, passes** (after Task 2 wires the calls; this task makes the renderer + CSS present). Commit `feat(dev-ui): inline thinking-step renderer + styles`.

---

### Task 2: Wire the stream to render inline steps (above the answer)

- [ ] **Step 1 — create the steps container + reset the map** — in the send handler, right after `const assistantDiv = addMsg('assistant', '', true);` (~line 1457), add:
```javascript
  for (const k in inlineSteps) delete inlineSteps[k];   // reset per send
  const stepsContainer = document.createElement('div');
  stepsContainer.className = 'inline-steps';
  chat.insertBefore(stepsContainer, assistantDiv);       // steps appear ABOVE the answer
```
- [ ] **Step 2 — render on each tool item** — in the `function_call` branch (~line 1504, alongside the existing `addEvent('tool-call', …)`), add:
```javascript
              renderInlineStep(stepsContainer, item.call_id || item.id || item.name,
                {{ name: item.name, phase: 'running', detail: argStr }});
```
  and in the `function_call_output` branch (~line 1514, alongside `addEvent('tool-result', …)`), add:
```javascript
              const isErr = /\"error\"|\berror\b/i.test(outStr);
              renderInlineStep(stepsContainer, item.call_id || item.id || item.name,
                {{ name: item.name || 'tool', phase: isErr ? 'error' : 'done', detail: outStr }});
```
  (The `function_call` carries `call_id`; the `function_call_output` carries the same `call_id` — that's the key that updates the SAME row from running→done. Confirm the field name on the streamed items via `_responses_agent._langchain_to_output_item` — it emits `call_id` for both.)
- [ ] **Step 3 — run `tests/test_dev_ui_routes.py::TestInlineSteps`** — passes. Also re-run `TestEventsToolCalls` (must still pass — Events unchanged) and `TestMarkdownWiring`/`TestLandingRender`.
- [ ] **Step 4 — commit** `feat(dev-ui): render live tool steps inline in the chat transcript`.

---

### Task 3: Gate

- [ ] `cd python && git checkout -- uv.lock 2>/dev/null || true`
- [ ] `cd python && uv run pytest -q` — all pass (baseline ~1848 + new).
- [ ] `cd python && uv run pyright src/apx_agent/_ui_chat.py` — 0 errors.
- [ ] `cd python && git checkout -- uv.lock 2>/dev/null || true && git status --short` — only `_ui_chat.py` + the test; uv.lock clean.

---

## Self-review

**Spec coverage:** expandable step rows (Task 1 renderer + CSS), live from the stream above the answer (Task 2 insertBefore + the two branches), action-steps-only (only function_call/output handled; no reasoning), Events unchanged (the #129 addEvent calls are kept alongside). Out-of-scope items untouched.

**Placeholder scan:** none — concrete JS/CSS/tests.

**Type/name consistency:** `renderInlineStep(stepsContainer, callId, opts)` defined in Task 1, called in Task 2; `inlineSteps` map declared by the renderer, reset in Task 2; `stepsContainer` created in Task 2. `call_id` is the running→done key.

**Verify-on-execute:** confirm the HTML-escape helper name (`esc`) used elsewhere in the file; confirm the streamed `function_call`/`function_call_output` items expose `call_id` (per `_langchain_to_output_item`); confirm `chat` is in scope in `renderInlineStep` (it is — `addMsg`/`addToolPills` reference it at module-script scope). Line numbers may have drifted — match the real `function_call` branch text from #129.
