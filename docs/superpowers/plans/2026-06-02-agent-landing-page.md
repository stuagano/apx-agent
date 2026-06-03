# Agent Landing Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deployed agent's empty-chat state with a landing that shows the agent's identity, capability cards (from its tools), and optional author-declared starter prompts — sourced from the declarative agent definition.

**Architecture:** One new UI-only config field (`AgentConfig.examples: list[str]`) parsed automatically by the existing loader; the dev-UI chat render (`_render_agent_ui`) gains a server-rendered landing block (greeting + capability cards from `ctx.tools` + starter chips from `ctx.config.examples`) with small JS for card-expand, chip-fill-input, and landing-removal-on-send; the scaffold ships sample examples. No runtime/serving behavior changes.

**Tech Stack:** Python, Pydantic (`AgentConfig`), the dev-UI HTML render in `_ui_chat.py`, click CLI scaffold. Tests via `cd python && uv run pytest`.

**Spec:** `docs/superpowers/specs/2026-06-02-agent-landing-page-design.md`

---

### Task 1: `AgentConfig.examples` field (parsed automatically)

The loader `_load_agent_config` (`_inspection.py:179`) builds `AgentConfig(**{k:v for k,v in section.items() if k in AgentConfig.model_fields})` — so adding the field is all that's needed for `examples = [...]` under `[tool.apx.agent]` to parse.

**Files:**
- Modify: `python/src/apx_agent/_models.py` (`AgentConfig`, after the `api_prefix` field ~line 173)
- Test: `python/tests/test_inspection.py`

- [ ] **Step 1: Write the failing test** — append to `tests/test_inspection.py`:

```python
def test_load_agent_config_parses_examples(tmp_path):
    from apx_agent._inspection import _load_agent_config

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.apx.agent]\n'
        'name = "demo"\n'
        'examples = ["Show me sample customers", "Top 5 by balance"]\n'
    )
    cfg = _load_agent_config(str(pyproject))
    assert cfg is not None
    assert cfg.examples == ["Show me sample customers", "Top 5 by balance"]


def test_load_agent_config_examples_defaults_empty(tmp_path):
    from apx_agent._inspection import _load_agent_config

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.apx.agent]\nname = "demo"\n')
    cfg = _load_agent_config(str(pyproject))
    assert cfg is not None
    assert cfg.examples == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_inspection.py -k examples -v`
Expected: FAIL — `examples` is dropped by the `model_fields` filter, so `cfg.examples` raises `AttributeError`.

- [ ] **Step 3: Add the field** — in `_models.py`, inside `class AgentConfig`, immediately after the `api_prefix: str = "/api"  # route prefix for tool endpoints` line, add:

```python
    examples: list[str] = []
    """Starter prompts shown on the dev-UI landing page (``[tool.apx.agent] examples``).

    UI-only metadata: surfaced to the chat landing as clickable starter chips;
    does not affect runtime agent behavior. Distinct from ``example`` (the
    declarative example *backend* config)."""
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_inspection.py -k examples -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_models.py python/tests/test_inspection.py
git commit -m "feat(config): add AgentConfig.examples (UI starter prompts)"
```

---

### Task 2: Render the landing in `_render_agent_ui`

**Files:**
- Modify: `python/src/apx_agent/_ui_chat.py` — `_render_agent_ui` (~line 414): add `examples` to the computed vars, add a `_render_landing` helper, swap the `#chat` initial content, add CSS, add JS (`useExample`, landing-removal on send).
- Test: `python/tests/test_dev_ui_routes.py`

- [ ] **Step 1: Write the failing test** — add to `tests/test_dev_ui_routes.py` (it already imports `AgentConfig, AgentContext` and has `_make_ctx`):

```python
class TestLandingRender:
    def _ctx(self, *, tools, examples):
        from apx_agent import AgentConfig, AgentContext
        from apx_agent._models import AgentTool
        cfg = AgentConfig(name="demo-agent", description="A demo agent.", examples=examples)
        tool_objs = [
            AgentTool(name=n, description=d, input_schema={"type": "object", "properties": {}})
            for n, d in tools
        ]
        card = {"name": "demo-agent", "description": "A demo agent.", "skills": []}
        return AgentContext(config=cfg, tools=tool_objs, card=card, agent=None)  # type: ignore[arg-type]

    def test_landing_shows_greeting_cards_and_chips(self):
        from apx_agent._ui_chat import _render_agent_ui
        html = _render_agent_ui(self._ctx(
            tools=[("sample_customer", "Preview rows."), ("run_sql", "Run SQL.")],
            examples=["Show me sample customers", "Top 5 by balance"],
        ))
        assert 'id="landing"' in html
        assert "demo-agent" in html and "A demo agent." in html
        assert "sample_customer" in html and "run_sql" in html      # capability cards
        assert "Show me sample customers" in html and "Top 5 by balance" in html  # chips

    def test_landing_no_tools_no_chips_still_has_greeting(self):
        from apx_agent._ui_chat import _render_agent_ui
        html = _render_agent_ui(self._ctx(tools=[], examples=[]))
        assert 'id="landing"' in html
        assert "demo-agent" in html
        assert 'class="cap-cards"' not in html       # no capability cards
        assert 'class="starter-chips"' not in html   # no chips

    def test_landing_examples_only_no_tools(self):
        from apx_agent._ui_chat import _render_agent_ui
        html = _render_agent_ui(self._ctx(tools=[], examples=["Hi there"]))
        assert 'class="starter-chips"' in html and "Hi there" in html
        assert 'class="cap-cards"' not in html
```

(Verify `AgentTool`'s real field name for the schema — `input_schema` per `_ui_chat.py:423` reads `t.input_schema`. If `AgentTool` uses a different attribute, match it.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py -k Landing -v`
Expected: FAIL — no `id="landing"` in the rendered page yet.

- [ ] **Step 3: Add the `_render_landing` helper** — in `_ui_chat.py`, above `_render_agent_ui`, add (and ensure `import html as _html` and `import json as _json` are available in the module/function):

```python
def _render_landing(ctx: "AgentContext") -> str:
    """Server-rendered empty-chat landing: greeting + capability cards + starter chips.

    Cards come from the agent's tools (click to expand params); chips come from
    ``ctx.config.examples`` (click fills the input). Each block renders only when
    its data is present; the greeting always renders.
    """
    import html as _html
    import json as _json

    name = ctx.config.name
    desc = ctx.config.description or ""
    tools = [t for t in ctx.tools if t.name != "create_tool"]
    examples = ctx.config.examples or []

    parts = [f'<div class="landing-hi">{_html.escape(name)}</div>']
    if desc:
        parts.append(f'<div class="landing-sub">{_html.escape(desc)}</div>')

    if tools:
        cards = "".join(
            '<div class="cap-card" onclick="this.classList.toggle(&quot;open&quot;)">'
            f'<div class="cap-name">{_html.escape(t.name)}</div>'
            f'<div class="cap-desc">{_html.escape(t.description or "")}</div>'
            f'<pre class="cap-params">{_html.escape(_json.dumps(t.input_schema or {"type": "object", "properties": {}}, indent=2))}</pre>'
            '</div>'
            for t in tools
        )
        parts.append('<div class="landing-label">What I can do</div>'
                     f'<div class="cap-cards">{cards}</div>')

    if examples:
        chips = "".join(
            f'<button type="button" class="starter-chip" onclick="useExample(this)" '
            f'data-q="{_html.escape(q, quote=True)}">{_html.escape(q)} →</button>'
            for q in examples
        )
        parts.append('<div class="landing-label">Try asking</div>'
                     f'<div class="starter-chips">{chips}</div>')

    return f'<div id="landing">{"".join(parts)}</div>'
```

- [ ] **Step 4: Swap the `#chat` initial content** — in `_render_agent_ui`, replace:

```python
    <div id="chat">
      <div class="msg system">Chat with <strong>{agent_name}</strong></div>
    </div>
```

with:

```python
    <div id="chat">
      {_render_landing(ctx) if ctx else f'<div class="msg system">Chat with <strong>{agent_name}</strong></div>'}
    </div>
```

- [ ] **Step 5: Add CSS** — in the `<style>` block of `_render_agent_ui` (near the `.empty-state` rule ~line 635), add:

```css
  #landing {{ padding: 28px 22px; max-width: 680px; }}
  .landing-hi {{ font-size: 19px; font-weight: 600; color: #fff; margin-bottom: 4px; }}
  .landing-sub {{ font-size: 13px; color: #8a929b; margin-bottom: 18px; line-height: 1.4; }}
  .landing-label {{ font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: #5d646c; margin: 16px 0 8px; }}
  .cap-cards {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .cap-card {{ background: #111418; border: 1px solid #262b31; border-radius: 8px; padding: 11px 13px; cursor: pointer; }}
  .cap-card:hover {{ border-color: #3a424b; }}
  .cap-name {{ color: #9ecbff; font-size: 12.5px; font-family: ui-monospace, monospace; }}
  .cap-desc {{ color: #9aa3ad; font-size: 11px; margin-top: 3px; line-height: 1.35; }}
  .cap-params {{ display: none; margin-top: 8px; padding-top: 8px; border-top: 1px solid #222;
                 color: #8a929b; font-size: 10.5px; white-space: pre-wrap; }}
  .cap-card.open .cap-params {{ display: block; }}
  .cap-card.open {{ border-color: #2f6b46; }}
  .starter-chip {{ display: inline-block; background: #15171a; border: 1px solid #2f343a; color: #bfe9cf;
                   border-radius: 16px; padding: 7px 13px; font-size: 12px; margin: 0 6px 7px 0; cursor: pointer; }}
  .starter-chip:hover {{ border-color: #2f6b46; }}
```

- [ ] **Step 6: Add JS** — `useExample` + remove-landing-on-send. Add the `useExample` function near the other top-level chat JS (after `const TOOLS = {tools_json};` ~line 720):

```javascript
function useExample(btn) {{
  const inp = document.getElementById('input');
  inp.value = btn.dataset.q;
  inp.focus();
}}
```

In the chat form submit handler (the `form.addEventListener('submit', ...)` / send path), add as the first line of the send action:

```javascript
  document.getElementById('landing')?.remove();
```

(Find the existing submit handler; add the removal so the landing disappears once the conversation starts. If messages are appended into `#chat`, removing `#landing` first keeps the transcript clean.)

- [ ] **Step 7: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_dev_ui_routes.py -k Landing -v`
Expected: PASS (all three).

- [ ] **Step 8: Commit**

```bash
git add python/src/apx_agent/_ui_chat.py python/tests/test_dev_ui_routes.py
git commit -m "feat(dev-ui): agent landing — greeting + capability cards + starter chips"
```

---

### Task 3: Scaffold ships sample `examples`

**Files:**
- Modify: `python/src/apx_agent/cli.py` — `_SCAFFOLD_APPS_PYPROJECT` `[tool.apx.agent]` block (after the `module = "agent:agent"` line, ~line 609)
- Test: `python/tests/test_cli.py`

- [ ] **Step 1: Write the failing test** — add to `tests/test_cli.py`:

```python
def test_scaffold_apps_pyproject_ships_examples() -> None:
    from apx_agent.cli import _SCAFFOLD_APPS_PYPROJECT
    assert "examples = [" in _SCAFFOLD_APPS_PYPROJECT
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd python && uv run pytest tests/test_cli.py -k ships_examples -v`
Expected: FAIL.

- [ ] **Step 3: Add the examples to the scaffold** — in `_SCAFFOLD_APPS_PYPROJECT`, after the `module = "agent:agent"` line, add:

```toml

# Starter prompts shown on the agent's landing page (clickable; fill the chat
# box so you can edit before sending). Tailor these to your agent's data/tools.
examples = [
    "Show me a few sample rows from the data you can access",
    "What questions can you answer?",
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd python && uv run pytest tests/test_cli.py -k ships_examples -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_cli.py
git commit -m "feat(scaffold): ship sample [tool.apx.agent] examples for the landing page"
```

---

### Task 4: Full-suite + pyright gate

- [ ] **Step 1: Restore uv.lock if poisoned**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true
```

- [ ] **Step 2: Full suite**

Run: `cd python && uv run pytest -q`
Expected: all pass (current baseline ~1824 passed, +new tests), 1 skipped.

- [ ] **Step 3: Pyright**

Run: `cd python && uv run pyright src/apx_agent/_models.py src/apx_agent/_ui_chat.py`
Expected: 0 errors. (`cli.py` is in the pyright exclude list but should import-clean.)

- [ ] **Step 4: Restore uv.lock + final check**

```bash
cd python && git checkout -- uv.lock 2>/dev/null || true && git status --short
```

Expected: only the intended source/test files modified; `uv.lock` clean.

---

## Self-review

**Spec coverage:** greeting + cards + chips (Task 2 ✓), `examples` DSL field (Task 1 ✓), graceful degradation (Task 2 tests: no-tools, examples-only ✓), scaffold examples (Task 3 ✓), UI-only/no-runtime-change (no `finalize_agent` touched ✓), testing (Tasks 1-3 ✓). No spec requirement is unimplemented.

**Placeholder scan:** none — every code step has concrete code.

**Type consistency:** `examples: list[str]` used identically in `_models.py`, `_load_agent_config` (auto), `_render_landing` (`ctx.config.examples`), and tests. `_render_landing(ctx)` reads `ctx.config.name/description/examples` and `ctx.tools[].name/description/input_schema` — matching `_render_agent_ui`'s existing usage at `_ui_chat.py:418-425`. Render fn is `_render_agent_ui` throughout.

**Known verify-on-execute:** confirm `AgentTool`'s schema attribute is `input_schema` (per `_ui_chat.py:423`) and the exact text of the `#chat` initial block + the submit-handler location before editing (re-inspect, line numbers may drift).
