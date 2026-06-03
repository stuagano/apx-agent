# Tool-Progress in Traces Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. TDD.

**Goal:** Surface tool progress in the dev-UI Trace tab — so a SQL-warehouse cold-start (and any tool's progress markers) shows up instead of the trace going silent during the wait.

**Design (approved):** (1) The warehouse cold-start wraps its start+wait in a **child span** (`safe_span`) so the trace shows a timed "SQL warehouse cold-start" step. (2) A general **`Dependencies.Progress`** injected callable lets any tool emit a **span event** (`emit_progress(message, **attrs)`) on the current active span. (3) The trace-detail renderer is **extended to display span events** (it currently drops them). All of this lives in the shared `compile_to_langgraph` tool-execution path / the trace renderer — **no streaming-adapter change**, so the serving-conformance contract (#125) is unaffected (Progress resolves in the shared `_make_dep_resolvers`, identical for both adapters).

**Tech Stack:** MLflow tracing (`safe_span`, `mlflow.get_current_active_span().add_event(...)`), the `Dependencies` DI pattern, the dev-UI trace renderer in `_dev.py`.

**Out of scope:** live Events-panel streaming (we chose trace span events, not SSE); changing streaming wire format; restyling the rest of the Trace UI.

---

### Task 1: `emit_progress` helper + warehouse cold-start child span

**Files:**
- Modify: `python/src/apx_agent/_mlflow_tracing.py` (add `emit_progress`)
- Modify: `python/src/apx_agent/_sql.py` (`_ensure_warehouse_running` ~line 82)
- Test: `python/tests/test_mlflow_tracing.py`, `python/tests/test_sql*.py` (find the sql test file; else add to `test_mlflow_tracing.py`)

- [ ] **Step 1: Failing test** for `emit_progress` (no active span → no-op; within a span → adds an event). In `tests/test_mlflow_tracing.py`:

```python
def test_emit_progress_noop_without_active_span():
    from apx_agent._mlflow_tracing import emit_progress
    # No active span / tracking — must not raise.
    emit_progress("hello", warehouse_id="wh-1")


def test_emit_progress_adds_event_to_active_span():
    import mlflow
    from apx_agent._mlflow_tracing import emit_progress

    with mlflow.start_span(name="parent") as span:
        emit_progress("Starting SQL warehouse", warehouse_id="wh-1")
    # The span recorded an event named for apx progress carrying the message.
    events = getattr(span, "events", None) or []
    assert any(
        getattr(e, "name", "") == "apx.progress"
        and (getattr(e, "attributes", {}) or {}).get("message") == "Starting SQL warehouse"
        for e in events
    )
```

(Verify the live MLflow span-event API before implementing: `mlflow.get_current_active_span()` returns the active span; add an event via `span.add_event(SpanEvent(name=..., attributes={...}))` — `from mlflow.entities import SpanEvent` — or `span.add_event(name, timestamp_ns=..., attributes=...)` depending on the installed mlflow version. Match what the version in `.venv` exposes; adapt the test's `events`/`attributes` access to the real SpanEvent shape.)

- [ ] **Step 2: Run → fails** (`emit_progress` undefined): `cd python && uv run pytest tests/test_mlflow_tracing.py -k emit_progress -v`

- [ ] **Step 3: Implement `emit_progress`** in `_mlflow_tracing.py`:

```python
def emit_progress(message: str, **attributes: Any) -> None:
    """Record a progress marker as an event on the current active MLflow span.

    Surfaces tool progress (e.g. a SQL-warehouse cold-start) in the trace
    without streaming. No-ops safely when MLflow is absent or there is no
    active span — never raises into the caller.
    """
    try:
        import mlflow
        from mlflow.entities import SpanEvent

        span = mlflow.get_current_active_span()
        if span is None:
            return
        attrs = {"message": message}
        attrs.update({k: str(v) for k, v in attributes.items()})
        span.add_event(SpanEvent(name="apx.progress", attributes=attrs))
    except Exception:  # pragma: no cover — tracing must never break a tool
        return
```

- [ ] **Step 4: Run → passes.**

- [ ] **Step 5: Failing test** for the cold-start span — assert `_ensure_warehouse_running` wraps the start in a span when the warehouse is STOPPED. In the sql test file, monkeypatch a fake `ws.warehouses` (get → STOPPED then RUNNING, start → noop) and patch `apx_agent._sql.safe_span` to a recorder; assert it was entered with a name containing "warehouse". (Match existing `_sql` test patterns for the fake ws.)

```python
def test_ensure_warehouse_running_opens_cold_start_span(monkeypatch):
    import apx_agent._sql as sql
    calls = []
    import contextlib
    @contextlib.contextmanager
    def fake_span(name, **kw):
        calls.append(name); yield
    monkeypatch.setattr(sql, "safe_span", fake_span)

    from databricks.sdk.service.sql import State
    from types import SimpleNamespace
    states = [State.STOPPED, State.RUNNING]
    ws = SimpleNamespace(warehouses=SimpleNamespace(
        get=lambda _id: SimpleNamespace(state=states.pop(0) if states else State.RUNNING),
        start=lambda _id: None,
    ))
    monkeypatch.setattr(sql.time, "sleep", lambda *_: None)
    sql._ensure_warehouse_running(ws, "wh-1", timeout_s=10)
    assert any("warehouse" in c.lower() for c in calls)
```

- [ ] **Step 6: Run → fails.**

- [ ] **Step 7: Implement** — in `_sql.py`, import `safe_span` and `emit_progress` from `._mlflow_tracing`; in `_ensure_warehouse_running`, when the warehouse is not RUNNING, wrap the start+wait loop in `with safe_span("SQL warehouse cold-start", attributes={"warehouse_id": warehouse_id}):` and call `emit_progress("Starting SQL warehouse — serverless cold-start, ~20-30s", warehouse_id=warehouse_id)` right after the existing `logger.warning(...)`. Keep all existing logging/behavior.

- [ ] **Step 8: Run → passes.** Then `git add python/src/apx_agent/_mlflow_tracing.py python/src/apx_agent/_sql.py python/tests/test_mlflow_tracing.py <sql test file>` and commit `feat(tracing): emit_progress span events + warehouse cold-start span`.

---

### Task 2: `Dependencies.Progress` injected emitter

The DI pattern (`_defaults.py`): a `_get_X` dep fn + `XDependency: TypeAlias = Annotated[..., Depends(_get_X)]` + `Dependencies.X = XDependency`, with the `_get_X` callable mapped to a value in `_compile.py:_make_dep_resolvers`.

**Files:**
- Modify: `python/src/apx_agent/_defaults.py` (add `ProgressFn`, `_get_progress`, `ProgressDependency`, `Dependencies.Progress`)
- Modify: `python/src/apx_agent/_compile.py` (`_make_dep_resolvers` ~line 100: register `_get_progress`)
- Test: `python/tests/test_compile.py` or `tests/test_defaults.py`

- [ ] **Step 1: Failing test** — a tool declaring `progress: Dependencies.Progress` resolves to a callable that, when called inside a span, emits an event. Simplest: assert `Dependencies.Progress` exists and the resolver maps `_get_progress` to `emit_progress`.

```python
def test_dependencies_progress_resolves_to_emitter():
    from apx_agent import Dependencies
    from apx_agent._defaults import _get_progress
    from apx_agent._mlflow_tracing import emit_progress
    assert Dependencies.Progress is not None
    # resolver wires _get_progress → emit_progress
    from apx_agent._compile import _make_dep_resolvers, CompileContext
    ctx = CompileContext(ws=None, model="m", headers=None)  # type: ignore[arg-type]
    resolvers = _make_dep_resolvers(ctx)
    assert resolvers[_get_progress] is emit_progress
```

- [ ] **Step 2: Run → fails.**

- [ ] **Step 3: Implement** — in `_defaults.py` (near `_get_principal`/`PrincipalDependency`):

```python
ProgressFn: TypeAlias = Callable[..., None]
"""Callable a tool calls to emit a progress marker into the trace."""


def _get_progress() -> ProgressFn:
    """Return the progress emitter (records a span event on the active span)."""
    from ._mlflow_tracing import emit_progress
    return emit_progress


ProgressDependency: TypeAlias = Annotated[ProgressFn, Depends(_get_progress)]
```

Add to `class Dependencies`:

```python
    Progress: TypeAlias = ProgressDependency
    """Emit a progress marker into the trace: ``progress("Loading…")``.
    Recommended usage: ``progress: Dependencies.Progress``."""
```

In `_compile.py`, import `_get_progress` (alongside `_get_principal`) and add to the dict returned by `_make_dep_resolvers`:

```python
        _get_progress: emit_progress,  # tool progress → trace span events
```

(import `emit_progress` from `._mlflow_tracing` at the top of `_compile.py` or inside `_make_dep_resolvers`.)

- [ ] **Step 4: Run → passes.** Commit `feat(deps): Dependencies.Progress — tools emit trace progress markers`.

---

### Task 3: Render span events in the Trace tab

**Files:**
- Modify: `python/src/apx_agent/_dev.py` — span_dicts (~line 696) + `_render_trace_detail` (~line 180)
- Test: `python/tests/test_dev_ui_routes.py` (or wherever `_render_trace_detail` is unit-testable)

- [ ] **Step 1: Failing test** — `_render_trace_detail` shows a span's events:

```python
def test_render_trace_detail_shows_span_events():
    from apx_agent._dev import _render_trace_detail
    spans = [{
        "span_id": "s1", "parent_id": None, "name": "run_sql",
        "span_type": "TOOL", "status": "OK",
        "start_time_ns": 0, "end_time_ns": 1_000_000, "duration_ms": 1.0,
        "inputs": None, "outputs": None,
        "events": [{"name": "apx.progress",
                    "attributes": {"message": "Starting SQL warehouse — serverless cold-start, ~20-30s"}}],
    }]
    html = _render_trace_detail("tr-1", spans, None)
    assert "Starting SQL warehouse" in html
```

- [ ] **Step 2: Run → fails** (renderer drops events).

- [ ] **Step 3: Implement** — in `_dev.py` span_dicts (~696), add to each span dict:

```python
                "events": [
                    {
                        "name": getattr(e, "name", ""),
                        "attributes": {k: str(v) for k, v in (getattr(e, "attributes", None) or {}).items()},
                    }
                    for e in (getattr(s, "events", None) or [])
                ],
```

In `_render_trace_detail` (~180-210), inside the per-span render loop, after the span's main row, render its events (use the `message` attribute when present):

```python
            for ev in (s.get("events") or []):
                _msg = (ev.get("attributes") or {}).get("message") or ev.get("name", "")
                body += f'<div class="span-event">▸ {_html_escape(_msg)}</div>'
```

(Use the module's existing HTML-escape helper — find how `_render_trace_detail` escapes elsewhere; if none, `import html` and use `html.escape`. Add a `.span-event` CSS rule in the trace-detail page styles: small, muted, indented.)

- [ ] **Step 4: Run → passes.** Commit `feat(dev-ui): render span events (tool progress) in the Trace tab`.

---

### Task 4: Gate

- [ ] `cd python && git checkout -- uv.lock 2>/dev/null || true`
- [ ] `cd python && uv run pytest -q` — all pass (baseline ~1834 + new, 1 skipped).
- [ ] `cd python && uv run pyright src/apx_agent/_mlflow_tracing.py src/apx_agent/_sql.py src/apx_agent/_defaults.py src/apx_agent/_compile.py src/apx_agent/_dev.py` — 0 errors.
- [ ] `cd python && git checkout -- uv.lock 2>/dev/null || true && git status --short` — only intended files; uv.lock clean.

---

## Self-review

**Spec coverage:** cold-start child span (Task 1 ✓), general `Dependencies.Progress` span events (Tasks 1-2 ✓), renderer shows events (Task 3 ✓), no streaming-adapter change / conformance intact (Progress in shared resolver ✓). Out-of-scope (live Events streaming) excluded.

**Placeholder scan:** none — concrete code throughout, with explicit verify-the-SpanEvent-API and find-the-escape-helper notes.

**Type/name consistency:** `emit_progress(message, **attrs)` defined in Task 1, referenced in `_get_progress` (Task 2) and `_make_dep_resolvers` (Task 2). `Dependencies.Progress` → `ProgressDependency` → `_get_progress`. `events` key added in Task 3 span_dicts matches the renderer + the test.

**Verify-on-execute:** the MLflow `SpanEvent` constructor/`add_event` signature for the installed version; the sql test file name + fake-ws pattern; the HTML-escape helper used in `_render_trace_detail`; that `CompileContext(ws=None,...)` is constructible for the resolver test (else build a minimal real ctx).
