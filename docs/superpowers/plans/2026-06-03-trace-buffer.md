# In-Process Trace Buffer + Fail-Fast Trace Detail — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. TDD. Steps use `- [ ]`.

**Goal:** Make the dev-UI Trace detail (`/_apx/traces/{id}`) work on FEVM/private-link, where `mlflow.get_trace()` hangs fetching span artifacts from blocked blob storage. Serve recent traces from an in-process ring buffer (captured when the trace is fresh in MLflow's in-memory buffer — no blob), and on a buffer miss, fall through to a **time-bounded** `get_trace` that fails fast with a clear message instead of hanging.

**Design (approved):**
- **#2 ring buffer:** a module-level bounded store (last 50 traces) of serialized spans, populated by capturing the trace right after each agent run (while it's still in MLflow's in-memory buffer — `mlflow.get_trace` reads in-memory first, so no blob round-trip), and ALSO populated opportunistically by the trace route on any successful fetch.
- **#1 fail-fast:** the route's fallback `get_trace` runs under a worker-thread timeout (~5s); on timeout/error it renders "span data unavailable on this workspace (artifact-storage egress blocked) — recent traces are served from memory."

**Conformance:** the capture hook goes in BOTH adapters' run paths via a shared helper, so they capture identically — the serving-conformance contract (#125) stays satisfied; add a conformance note/test if the hook is adapter-visible.

**Caveats (state in the route's empty message + docs):** per-process buffer (multi-replica → miss → fail-fast); recent-only (since app start).

**KEY EMPIRICAL RISK — verify before finalizing capture:** which trace the dev UI displays (the autolog LangGraph trace with TOOL spans) and *when* it is complete in MLflow's in-memory buffer. The capture must yield COMPLETE spans. The gating test (Task 4) runs a real agent and asserts the buffer ends up holding that trace WITH its spans — **iterate the capture point until that test passes.**

---

### Task 1: Shared span serializer

Extract the inline span→dict serialization at `_dev.py:705-707` into a reusable helper so the buffer and the route produce identical span dicts.

**Files:** Modify `python/src/apx_agent/_dev.py`; Test `python/tests/test_dev_ui_routes.py`.

- [ ] **Step 1 — failing test:**
```python
def test_serialize_trace_spans_shape():
    from apx_agent._dev import _serialize_trace_spans
    from types import SimpleNamespace
    span = SimpleNamespace(
        span_id="s1", parent_id=None, name="run_sql",
        span_type=SimpleNamespace(value="TOOL"),
        status=SimpleNamespace(status_code=SimpleNamespace(value="OK")),
        start_time_ns=0, end_time_ns=1_000_000, inputs={"q": "x"}, outputs=None,
        events=[SimpleNamespace(name="apx.progress", attributes={"message": "hi"})],
    )
    trace = SimpleNamespace(data=SimpleNamespace(spans=[span]))
    out = _serialize_trace_spans(trace)
    assert out[0]["name"] == "run_sql"
    assert out[0]["span_type"] == "TOOL"
    assert out[0]["events"][0]["attributes"]["message"] == "hi"
```
- [ ] **Step 2 — run, fails.**
- [ ] **Step 3 — implement** `_serialize_trace_spans(trace) -> list[dict]` in `_dev.py` containing the exact logic currently inline at lines ~695-707 (including the `events` extraction added in #128). Replace the inline block in `trace_detail_ui` with `span_dicts = _serialize_trace_spans(trace)`.
- [ ] **Step 4 — run, passes.** Commit `refactor(dev-ui): extract _serialize_trace_spans helper`.

---

### Task 2: The ring buffer + capture helper

**Files:** Create `python/src/apx_agent/_trace_store.py`; Test `python/tests/test_trace_store.py`.

- [ ] **Step 1 — failing test:**
```python
def test_trace_store_put_get_and_bound():
    from apx_agent import _trace_store as ts
    ts.reset()
    ts.put("tr-1", [{"name": "a"}])
    assert ts.get("tr-1") == [{"name": "a"}]
    assert ts.get("nope") is None
    for i in range(ts.MAX_TRACES + 10):
        ts.put(f"x-{i}", [{"name": str(i)}])
    assert len(ts._STORE) <= ts.MAX_TRACES          # bounded (oldest evicted)
    assert ts.get("tr-1") is None                    # tr-1 evicted
```
- [ ] **Step 2 — run, fails.**
- [ ] **Step 3 — implement** `_trace_store.py`: a thread-safe bounded `OrderedDict` `_STORE` (`MAX_TRACES = 50`), with `put(trace_id, span_dicts)` (move-to-end + popitem(last=False) when over cap), `get(trace_id) -> list | None`, and `reset()`. Use a module `threading.Lock`.
- [ ] **Step 4 — run, passes.** Commit `feat(dev-ui): in-process trace ring buffer`.

---

### Task 3: Capture traces after each agent run

Add a shared `capture_current_trace()` that grabs the just-finished trace from MLflow's in-memory buffer and stores its serialized spans. Call it from BOTH adapters right after the run completes.

**Files:** Modify `python/src/apx_agent/_trace_store.py` (add `capture_current_trace`), `python/src/apx_agent/_responses_agent.py` (after `non_streaming` assembles `response` ~709, and at the end of `streaming` ~791), `python/src/apx_agent/_chat_agent.py` (after `predict` ~399 and `predict_stream`). Test: `python/tests/test_trace_store.py`.

- [ ] **Step 1 — implement `capture_current_trace()`** in `_trace_store.py`:
```python
def capture_current_trace() -> str | None:
    """Best-effort: snapshot the just-finished trace from MLflow's in-memory
    buffer into the ring store (no blob fetch). Returns the trace_id or None.
    Never raises into the caller."""
    try:
        import mlflow
        from ._dev import _serialize_trace_spans
        trace_id = mlflow.get_last_active_trace_id()
        if not trace_id:
            return None
        trace = mlflow.get_trace(trace_id, silent=True)   # in-memory first
        spans = _serialize_trace_spans(trace) if trace is not None else []
        if spans:
            put(trace_id, spans)
        return trace_id
    except Exception:
        return None
```
  (Avoid an import cycle: import `_serialize_trace_spans` lazily inside the function, as shown. If `_dev` importing `_trace_store` would also cycle, keep the serializer import lazy on both sides.)
- [ ] **Step 2 — wire the call** into both adapters, AFTER the run is complete and the top-level span context has exited (so the trace is finalized). In `_responses_agent.non_streaming`, call `capture_current_trace()` just before `return response` — **but verify (Task 4) the trace is complete there**; if not, move it after the outermost `safe_span` closes. Same for `streaming` (after the loop / terminal event) and `_chat_agent.predict`/`predict_stream`. Use one shared call site pattern in each.
- [ ] **Step 3 — VERIFY EMPIRICALLY (gating):** write `tests/test_trace_store.py::test_capture_after_real_agent_run` that builds a tiny agent with a no-arg tool, runs it through the ResponsesAgent non-streaming path with autolog enabled (mirror `test_serving_conformance.py` / `test_responses_agent.py` stubs but let MLflow tracing run against a local file/sqlite tracking dir), then asserts `_trace_store.get(<trace_id>)` returns spans including the tool span. If the captured trace lacks spans, the capture point is wrong — move it (after span close / use the autolog trace id) until this passes. Document the working capture point in a comment.
- [ ] **Step 4 — run, passes.** Commit `feat(dev-ui): capture traces in-process after each agent run`.

---

### Task 4: Serve from buffer + fail-fast fallback in the route

**Files:** Modify `python/src/apx_agent/_dev.py` (`trace_detail_ui` ~688); Test `python/tests/test_dev_ui_routes.py`.

- [ ] **Step 1 — failing tests** (async route tests, match the file's style):
  - buffer hit → 200, spans served, **`mlflow.get_trace` NOT called** (patch it to assert not-called / to raise if called).
  - buffer miss + `get_trace` slow (patch to sleep > timeout) → fast 200/2xx with the "unavailable" message, NOT a 60s hang (assert it returns within a few seconds).
  - buffer miss + `get_trace` ok → serves spans AND populates the buffer (subsequent `get(id)` is non-None).
- [ ] **Step 2 — run, fails.**
- [ ] **Step 3 — implement** in `trace_detail_ui`: first `from ._trace_store import get as _ts_get, put as _ts_put`; if `_ts_get(trace_id)` returns spans, render/JSON from them (skip `get_trace`). On miss, run `mlflow.get_trace(trace_id)` inside a `concurrent.futures.ThreadPoolExecutor(max_workers=1)` with `fut.result(timeout=5)`; on `FuturesTimeout`/exception, render the "span data unavailable on this workspace (artifact-storage egress blocked) — recent traces are served from memory" message (fmt-aware: JSON `{"error": ...}` vs HTML via `_render_trace_detail`). On success, `_serialize_trace_spans` → `_ts_put(trace_id, spans)` → render.
- [ ] **Step 4 — run, passes.** Commit `feat(dev-ui): serve traces from buffer; fail-fast on blocked blob fetch`.

---

### Task 5: Gate

- [ ] `cd python && git checkout -- uv.lock 2>/dev/null || true`
- [ ] `cd python && uv run pytest -q` — all pass (baseline + new). **Includes the conformance suite `test_serving_conformance.py` — must stay green** (capture is via a shared helper; both adapters identical).
- [ ] `cd python && uv run pyright src/apx_agent/_dev.py src/apx_agent/_trace_store.py src/apx_agent/_responses_agent.py src/apx_agent/_chat_agent.py` — 0 errors.
- [ ] `cd python && git checkout -- uv.lock 2>/dev/null || true && git status --short` — only intended files; uv.lock clean.

---

## Self-review

**Spec coverage:** ring buffer (Task 2), capture after run (Task 3), serve-from-buffer + fail-fast (Task 4), shared serializer for identical shape (Task 1), conformance kept (Task 5 runs the contract). #1 (fail-fast) = Task 4 bounded `get_trace`. #2 (buffer) = Tasks 2-4.

**Placeholder scan:** none — concrete code/tests. The capture point is explicitly empirical with a gating test (Task 3 Step 3).

**Type/name consistency:** `_serialize_trace_spans(trace)` (Task 1) used by `capture_current_trace` (Task 3) + the route (Task 4). `_trace_store`: `put/get/reset/MAX_TRACES/_STORE/capture_current_trace`. Route imports match.

**Verify-on-execute (the crux):** the capture point (which trace id + when complete). The Task-3 gating test is the arbiter — DO NOT finalize until a real agent run leaves the buffer holding the trace WITH tool spans. If `mlflow.get_last_active_trace_id()` returns the apx `safe_span` trace rather than the autolog LangGraph trace, capture the right one (the one the dev UI's finalizeTrace fetches) — inspect how `x-return-trace-id` / the streamed `trace_id` is produced and mirror it.
