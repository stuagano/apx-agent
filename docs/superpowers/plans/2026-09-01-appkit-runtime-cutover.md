# AppKit Runtime Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the generated AppKit host the default Databricks Apps runtime for the surface AppKit natively supports, without adding state, thread-store, or dynamic-approval adapters.

**Architecture:** AppKit owns public agent transport, threads, streaming, cancellation, and static effect-based HITL. The existing loopback Python process executes stateless declared tools and serves the existing APX auxiliary routes; the generated Node host proxies an explicit route allowlist. Unsupported `Dependencies.State` and APX `ASK` behavior fails closed, while `APX_APPS_HOST=python` remains the one-release rollback path.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic, TypeScript 5.9, Databricks AppKit 0.66.1, Vitest, pytest, Databricks CLI 1.12.1.

**Spec:** `docs/superpowers/specs/2026-09-01-appkit-runtime-parity-design.md`

## Global Constraints

- Use only AppKit's native static effect-based approval; do not add a policy-preflight protocol, second approval store, custom `ThreadStore`, or AppKit fork.
- Keep `Dependencies.State` unsupported under AppKit and return a bounded error before tool execution.
- Preserve forwarded Databricks Apps identity and token headers without logging their values.
- Default unknown Python tool effects to `update`, never `read`.
- Proxy only named APX auxiliary paths; do not add a catch-all proxy or configurable empty default.
- Run every Databricks CLI command with `--profile fevm`; ignore ambient `DATABRICKS_CONFIG_PROFILE`.
- Keep Python as the default until Tasks 1-5 and the live proof pass.
- Add no production dependency, storage service, or public listener.

---

## File Map

- `python/src/apx_agent/_tool.py`: extend the existing `ToolMetadata` and `@tool` decorator with AppKit's `effect` value.
- `python/src/apx_agent/_apps_host_manifest.py`: project tool effects into the existing host manifest with a conservative `update` default.
- `python/src/apx_agent/_appkit_tool_bridge.py`: keep stateless Python execution, normalize policy denial/unsupported `ASK`, and preserve stateful-tool rejection.
- `typescript/src/internal/appkit-host.ts`: remove the duplicate optional TypeScript policy/audit layer; keep manifest tool dispatch and AppKit annotations.
- `python/src/apx_agent/cli.py`: generate the sidecar from normal APX app wiring and stage AppKit by default only in the final cutover.
- `python/src/apx_agent/_appkit_host_generator.py`: generate the fixed auxiliary-route proxy and retain the two-child lifecycle.
- `python/src/apx_agent/_project_gen.py`: select AppKit when `APX_APPS_HOST` is absent after the cutover gate.
- Existing Python and TypeScript test files: prove each supported behavior through real route/plugin boundaries.

---

### Task 1: Project native AppKit tool effects without a new metadata type

**Files:**
- Modify: `python/src/apx_agent/_tool.py:55-220`
- Modify: `python/src/apx_agent/_apps_host_manifest.py:44-186`
- Test: `python/tests/test_tool.py`
- Test: `python/tests/test_apps_host_manifest.py`

**Interfaces:**
- Consumes: existing `ToolMetadata`, `tool()`, and `get_tool_metadata(fn)`.
- Produces: `ToolMetadata.effect: Literal["read", "write", "update", "destructive"] | None` and manifest `annotations.effect` with an effective default of `update`.

- [ ] **Step 1: Write decorator tests for explicit and absent effects**

Add tests that use the public decorator rather than mutating function attributes:

```python
def test_tool_records_appkit_effect() -> None:
    @tool(effect="read")
    def lookup(value: str) -> str:
        return value

    assert get_tool_metadata(lookup).effect == "read"


def test_tool_without_effect_keeps_metadata_unspecified() -> None:
    @tool
    def apply(value: str) -> str:
        return value

    assert get_tool_metadata(apply).effect is None
```

Also parameterize invalid values and assert decoration raises `ValueError` naming the four accepted values.

- [ ] **Step 2: Run the decorator tests and verify failure**

Run:

```bash
cd python && uv run --frozen pytest tests/test_tool.py -k effect -q
```

Expected: failure because `tool()` does not accept `effect` and `ToolMetadata` has no `effect` field.

- [ ] **Step 3: Extend the existing metadata and decorator**

Add one alias and one field; do not add a second annotation class:

```python
from typing import Literal

ToolEffect = Literal["read", "write", "update", "destructive"]
_TOOL_EFFECTS = frozenset({"read", "write", "update", "destructive"})

@dataclass(frozen=True)
class ToolMetadata:
    # existing fields stay unchanged
    effect: ToolEffect | None = None
```

Add `effect: ToolEffect | None` to both overload and implementation signatures, validate membership before decoration, and pass `effect=effect` into the existing `ToolMetadata(...)` construction. Document that `None` means the AppKit manifest will conservatively use `update`.

- [ ] **Step 4: Write manifest projection tests**

Add one explicitly read-only tool and one plain function:

```python
@tool(effect="read")
def lookup(value: str) -> str:
    return value

def apply(value: str) -> str:
    return value

manifest = compile_apps_host_manifest(LlmAgent(tools=[lookup, apply]))
effects = {item.name: item.annotations.effect for item in manifest.tools}
assert effects == {"lookup": "read", "apply": "update"}
```

- [ ] **Step 5: Run the manifest test and verify failure**

Run:

```bash
cd python && uv run --frozen pytest tests/test_apps_host_manifest.py -q
```

Expected: the plain tool currently projects as `read`, and explicit metadata is ignored.

- [ ] **Step 6: Project the effective effect**

Import `ToolEffect` and `get_tool_metadata`. Constrain `AppsHostToolAnnotations.effect` to `ToolEffect` and construct annotations in `_tool_manifest`:

```python
metadata = get_tool_metadata(fn)
effect = metadata.effect if metadata and metadata.effect is not None else "update"
return AppsHostTool(
    # existing fields
    annotations=AppsHostToolAnnotations(effect=effect),
)
```

- [ ] **Step 7: Run focused Python tests**

Run:

```bash
cd python && uv run --frozen pytest tests/test_tool.py tests/test_apps_host_manifest.py -q
```

Expected: pass.

- [ ] **Step 8: Commit the effect projection**

```bash
git add python/src/apx_agent/_tool.py python/src/apx_agent/_apps_host_manifest.py python/tests/test_tool.py python/tests/test_apps_host_manifest.py
git commit -m "feat: project AppKit tool effects"
```

---

### Task 2: Keep one Python governance boundary and fail unsupported decisions closed

**Files:**
- Modify: `python/src/apx_agent/_appkit_tool_bridge.py:1-100`
- Modify: `typescript/src/internal/appkit-host.ts:37-118,280-405`
- Test: `python/tests/test_appkit_tool_bridge.py`
- Test: `typescript/tests/appkit-host.test.ts`

**Interfaces:**
- Consumes: the existing Python `before_tool`/`after_tool` callback handler and AppKit `ToolProvider` dispatch.
- Produces: bridge `403` responses for `PermissionError`/APX approval-required outcomes, unchanged `400` stateful rejection, and no duplicate TypeScript policy/audit callbacks.

- [ ] **Step 1: Write Python fail-closed bridge tests**

Add tests proving the function body does not run:

```python
def test_bridge_returns_403_when_before_tool_denies(monkeypatch) -> None:
    called = False

    def mutate(value: str) -> str:
        nonlocal called
        called = True
        return value

    agent = LlmAgent(
        tools=[mutate],
        before_tool=lambda _name, _args: (_ for _ in ()).throw(PermissionError("blocked")),
    )
    response = TestClient(_app(agent, monkeypatch)).post(
        "/_apx/internal/appkit/tools/mutate",
        json={"args": {"value": "x"}},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "blocked"}
    assert called is False
```

Use the existing `ApprovalRequired` construction in `python/tests/test_policy.py` for a second test and assert the bridge returns a bounded `403` without approval IDs or a retry protocol.

- [ ] **Step 2: Run the bridge tests and verify failure**

Run:

```bash
cd python && uv run --frozen pytest tests/test_appkit_tool_bridge.py -q
```

Expected: FastAPI currently surfaces callback governance exceptions as `500` or raises them through `TestClient`.

- [ ] **Step 3: Normalize only governance exceptions**

Wrap `lc_tool.ainvoke(...)` in `_appkit_tool_bridge.py`:

```python
try:
    result = await lc_tool.ainvoke(body.args, config=config)
except PermissionError as exc:
    raise HTTPException(status_code=403, detail=str(exc)) from exc
return {"result": result}
```

Do not catch `Exception`; validation errors and programming defects must retain their existing error behavior. Keep the pre-invocation `_state_param_name` rejection unchanged.

- [ ] **Step 4: Delete the unused TypeScript policy/audit path**

Remove `InternalApxAppKitPolicyAction`, policy/audit event and decision interfaces, `policy`/`audit` config fields, and their calls from `executeAgentTool`. Manifest-backed tools should perform exactly one `fetch` to Python. Local TypeScript-native agent exports should call their declared handler directly.

Replace the existing policy/audit test with:

```typescript
it('does not add a second policy layer around APX tool execution', async () => {
  const apx = new InternalApxAppKitGovernancePlugin({ agent: makeAgentExports() });
  await expect(
    apx.executeAgentTool('lookup_policy', { resource: 'main.sales.orders' }),
  ).resolves.toEqual({ resource: 'main.sales.orders', policy: 'read-only' });
});
```

Update manifest bridge tests to stop constructing or asserting audit events.

- [ ] **Step 5: Run focused Python and TypeScript tests**

Run:

```bash
cd python && uv run --frozen pytest tests/test_appkit_tool_bridge.py -q
cd typescript && npm test -- tests/appkit-host.test.ts
```

Expected: pass.

- [ ] **Step 6: Commit the governance simplification**

```bash
git add python/src/apx_agent/_appkit_tool_bridge.py python/tests/test_appkit_tool_bridge.py typescript/src/internal/appkit-host.ts typescript/tests/appkit-host.test.ts
git commit -m "fix: fail AppKit tool governance closed"
```

---

### Task 3: Serve existing APX auxiliary routes through a fixed proxy

**Files:**
- Modify: `python/src/apx_agent/cli.py:1606-1641`
- Modify: `python/src/apx_agent/_appkit_host_generator.py:189-360`
- Test: `python/tests/test_appkit_host_generator.py`
- Test: `python/tests/test_appkit_examples.py`

**Interfaces:**
- Consumes: `apx_agent.create_app(agent)`, which already mounts the private tool bridge, MCP, A2A, feedback, dev/topology/trace, health, and readiness routes.
- Produces: generated Node routes for `/_apx`, `/mcp`, `/.well-known/agent.json`, `POST /`, and `/readyz` that stream to `APX_PYTHON_BRIDGE_URL`.

- [ ] **Step 1: Write the sidecar-wiring test**

Change the generated bridge assertions to require normal APX wiring:

```python
bridge_src = (bridge_dir / "appkit_bridge.py").read_text()
assert "from apx_agent import create_app" in bridge_src
assert "app = create_app(agent)" in bridge_src
assert "FastAPI()" not in bridge_src
assert "finalize_agent(" not in bridge_src
```

This proves the generator reuses the existing route lifecycle instead of reconstructing it.

- [ ] **Step 2: Run the generator/example tests and verify failure**

Run:

```bash
cd python && uv run --frozen pytest tests/test_appkit_host_generator.py tests/test_appkit_examples.py -q
```

Expected: current generated bridge creates a tool-only `FastAPI` app.

- [ ] **Step 3: Replace the tool-only generated bridge**

Reduce `_INTERNAL_APPKIT_BRIDGE_SERVER` to:

```python
"""Loopback APX sidecar for the generated AppKit host."""
from apx_agent import create_app
from agent import agent

app = create_app(agent)
```

Do not mount the tool router again; `create_app` already does so in `_wiring.setup_agent`.

- [ ] **Step 4: Write fixed-proxy generator tests**

Assert generated `server.ts` contains the named routes and no environment-controlled proxy list:

```python
assert "APX_PYTHON_BRIDGE_PROXY_PATHS" not in server_ts
assert "app.use('/_apx', proxyToPython)" in server_ts
assert "app.use('/mcp', proxyToPython)" in server_ts
assert "app.get('/.well-known/agent.json', proxyToPython)" in server_ts
assert "app.post('/', proxyToPython)" in server_ts
assert "app.get('/readyz', proxyToPython)" in server_ts
```

Also assert the proxy deletes `host`, `connection`, `transfer-encoding`, and `content-length` before forwarding or returning upstream headers, and emits only `{"detail":"APX Python bridge unavailable"}` on connection failure.

- [ ] **Step 5: Generate one small proxy function and fixed registrations**

In `_server_ts`, replace `proxyPaths` and its loop with one `proxyToPython(req, res)` function. Use Node's installed `http` module, preserve `req.originalUrl`, pipe request and response bodies, and register exactly:

```typescript
app.use('/_apx', proxyToPython);
app.use('/mcp', proxyToPython);
app.get('/.well-known/agent.json', proxyToPython);
app.post('/', proxyToPython);
app.get('/readyz', proxyToPython);
```

Register these before AppKit's static fallback in the existing `server.extend` callback. Do not proxy `/health`, `/chat`, `/responses`, `/invocations`, or AppKit thread routes.

- [ ] **Step 6: Add a generated-host runtime probe**

Extend the example subprocess probe to start a local fake Python upstream, issue one JSON request and one streaming response through the generated proxy, then assert method, query string, body, status, content type, and forwarded identity header are preserved. Stop the fake upstream and assert `/readyz` returns bounded `502`.

- [ ] **Step 7: Run focused generation and integration tests**

Run:

```bash
cd python && uv run --frozen pytest tests/test_appkit_host_generator.py tests/test_appkit_examples.py -q
```

Expected: pass.

- [ ] **Step 8: Commit the route reuse**

```bash
git add python/src/apx_agent/cli.py python/src/apx_agent/_appkit_host_generator.py python/tests/test_appkit_host_generator.py python/tests/test_appkit_examples.py
git commit -m "feat: proxy APX routes from AppKit host"
```

---

### Task 4: Prove the supported AppKit surface locally while Python remains default

**Files:**
- Modify: `python/tests/test_appkit_examples.py`
- Modify: `python/tests/test_deploy_apps.py`
- Modify: `typescript/tests/appkit-host.test.ts`
- Modify only if an uncovered defect requires it: files owned by Tasks 1-3

**Interfaces:**
- Consumes: explicit `APX_APPS_HOST=appkit` staging and the generated Node/Python runtime.
- Produces: a local parity test that exercises supported behavior through public AppKit and private APX boundaries without changing the default.

- [ ] **Step 1: Add one explicit supported-surface fixture**

The fixture declaration must contain:

```python
@tool(effect="read")
def who_am_i(headers: Dependencies.Headers) -> str:
    return headers.user_id or "missing"

@tool(effect="update")
def apply_change(value: str) -> str:
    return f"applied:{value}"

def remember(value: str, state: Dependencies.State) -> str:
    state["value"] = value
    return value
```

Attach a `before_tool` hook that denies `apply_change` for one argument and an `after_tool` spy for successful calls.

- [ ] **Step 2: Exercise the real AppKit testing context**

Use `createTestPluginContext` and `createMockRequest` to prove OBO headers reach the Python fetch, explicit effects reach AppKit definitions, cancellation passes an `AbortSignal`, bridge `403` details remain bounded, and the stateful tool receives the explicit unsupported response without running.

- [ ] **Step 3: Exercise generated build and process lifecycle**

Stage with explicit AppKit selection, run `npm install --ignore-scripts` and `npm run build` inside the generated host, start it on local ports, wait for `/health`, verify proxied `/readyz`, then terminate it and assert both child processes exit. Add a child-failure case and assert the supervisor exits non-zero.

- [ ] **Step 4: Run the complete AppKit-focused suite**

Run:

```bash
cd typescript && npm run build && npm test -- tests/appkit-host.test.ts
cd python && uv run --frozen pytest tests/test_appkit_tool_bridge.py tests/test_appkit_host_generator.py tests/test_appkit_examples.py tests/test_deploy_apps.py -q
```

Expected: pass with Python still selected when `APX_APPS_HOST` is absent.

- [ ] **Step 5: Run the repository read-after-write gate**

Run:

```bash
make check
cd python && uv run --frozen pytest
```

Expected: all configured tests pass; existing repository skips, if any, are reported rather than newly introduced.

- [ ] **Step 6: Commit only additional test-driven corrections**

If Step 5 required a correction, stage only the affected Task 1-3 files and their tests:

```bash
git add python/src/apx_agent python/tests typescript/src/internal/appkit-host.ts typescript/tests/appkit-host.test.ts
git commit -m "test: prove supported AppKit runtime surface"
```

If no correction was needed, do not create an empty commit.

---

### Task 5: Run the disposable live proof on `fevm`

**Files:**
- Create: `docs/superpowers/verification/2026-09-01-appkit-fevm-proof.md`
- Modify only if the live proof exposes a confirmed defect: the smallest owning file from Tasks 1-3 and its focused test.

**Interfaces:**
- Consumes: explicit AppKit deployment behavior from Tasks 1-4.
- Produces: a redacted verification receipt for the `contract-parsing-agent` app; no default cutover yet.

- [ ] **Step 1: Reconfirm the exact profile and app before mutation**

Run:

```bash
databricks current-user me --profile fevm --output json
databricks apps get contract-parsing-agent --profile fevm --output json
```

Record only user identity, app name, app state, and non-secret resource identifiers. Do not record tokens or authorization headers.

- [ ] **Step 2: Deploy the branch build with explicit AppKit selection**

Set `APX_APPS_HOST=appkit` in the disposable app's bundle configuration, then use the repository's existing deployment command with `--profile fevm`. Do not rely on ambient profile state. Capture the deployment ID/commit, app URL, and readiness result.

- [ ] **Step 3: Verify authenticated and OBO behavior**

Open the app through its authenticated URL. Run the read fixture and confirm the downstream SQL identity matches the signed-in user. Record the query shape and returned principal, not credentials.

- [ ] **Step 4: Verify AppKit approval behavior**

Invoke the `update` fixture through `/chat`, deny once and prove the Python body did not run, then approve a new call and prove it ran exactly once. Confirm non-streaming `/invocations` returns AppKit's documented `400` when approval-requiring tools are available.

- [ ] **Step 5: Verify auxiliary routes**

Check `/.well-known/agent.json`, `/mcp`, `POST /` A2A JSON-RPC, `/_apx/feedback`, `/_apx/topology.json`, `/_apx/traces`, and `/readyz`. Use non-mutating requests except for the explicitly approved feedback fixture.

- [ ] **Step 6: Inspect logs for bounded failures and secret safety**

Run the Databricks Apps log command with `--profile fevm`. Search locally for authorization schemes, forwarded token header names followed by values, and known test token sentinels. The receipt states pass/fail and redacts any accidental secret before it is written.

- [ ] **Step 7: Write and commit the proof receipt**

The receipt contains exact commands, timestamps, app/deployment identifiers, observed statuses, and redacted evidence for Steps 1-6. It must explicitly list stateful tools, APX dynamic `ASK`, and durable AppKit threads as unsupported rather than passed.

```bash
git add -f docs/superpowers/verification/2026-09-01-appkit-fevm-proof.md
git commit -m "docs: record AppKit fevm verification"
```

---

### Task 6: Cut over the missing-environment default and preserve rollback

**Files:**
- Modify: `python/src/apx_agent/cli.py:7930-7938`
- Modify: `python/src/apx_agent/_project_gen.py:242-281`
- Modify: `python/tests/test_deploy_apps.py:306-410`
- Modify: `python/tests/test_project_gen.py`
- Modify: `python/tests/test_scaffold_apps.py`
- Modify: `python/tests/test_appkit_examples.py`
- Modify: `docs/design/apx-internal-appkit-host.md`

**Interfaces:**
- Consumes: passing local gates and committed live proof from Tasks 1-5.
- Produces: missing `APX_APPS_HOST` selects AppKit; explicit `APX_APPS_HOST=python` selects the prior Python host.

- [ ] **Step 1: Reverse the safety-baseline tests first**

Update tests to assert:

```python
def test_appkit_is_staged_when_host_env_is_missing(...):
    # no APX_APPS_HOST entry
    _stage_internal_appkit_host(...)
    assert (tmp_path / ".build" / "apx_appkit_host" / "package.json").exists()


def test_explicit_python_host_skips_appkit_staging(...):
    # APX_APPS_HOST=python
    _stage_internal_appkit_host(...)
    assert not (tmp_path / ".build" / "apx_appkit_host").exists()
```

For `_START_HOST_CONTENT`, execute the generated selector with mocked `os.execvp`/`os.execvpe` and assert missing env reaches npm/AppKit while explicit `python` reaches uvicorn.

- [ ] **Step 2: Run the default-selection tests and verify failure**

Run:

```bash
cd python && uv run --frozen pytest tests/test_deploy_apps.py tests/test_project_gen.py tests/test_scaffold_apps.py tests/test_appkit_examples.py -k 'appkit or python_host or host_env' -q
```

Expected: missing environment still selects Python.

- [ ] **Step 3: Change exactly the two defaults**

In `cli._stage_internal_appkit_host`:

```python
host = (_apps_config_env_value(doc, bundle_key, "APX_APPS_HOST") or "appkit")
```

In `_START_HOST_CONTENT`:

```python
host = os.environ.get("APX_APPS_HOST", "appkit").strip().lower()
```

Keep the explicit `python` and invalid-value branches unchanged.

- [ ] **Step 4: Update the internal design document**

State that AppKit is the generated default, Python is a one-release rollback selector, and the accepted unsupported behaviors are stateful tools, dynamic APX `ASK`, and durable AppKit threads. Remove claims that these are pending parity blockers.

- [ ] **Step 5: Run all affected tests**

Run:

```bash
cd python && uv run --frozen pytest tests/test_deploy_apps.py tests/test_project_gen.py tests/test_scaffold_apps.py tests/test_appkit_examples.py -q
```

Expected: pass.

- [ ] **Step 6: Run the final repository gates**

Run:

```bash
cd typescript && npm ci && npm run build
make check
cd python && uv run --frozen pytest
```

Expected: all configured tests pass and the lockfile registry checks remain clean.

- [ ] **Step 7: Review the final branch boundary**

Run:

```bash
git status --short
git log --oneline --decorate origin/main..HEAD
git diff --stat origin/main...HEAD
git diff --name-only origin/main...HEAD
```

Expected: only the safety baseline, approved design/plan, AppKit supported-surface implementation, live proof, and default cutover are present.

- [ ] **Step 8: Commit the cutover**

```bash
git add python/src/apx_agent/cli.py python/src/apx_agent/_project_gen.py python/tests/test_deploy_apps.py python/tests/test_project_gen.py python/tests/test_scaffold_apps.py python/tests/test_appkit_examples.py docs/design/apx-internal-appkit-host.md
git commit -m "feat: make AppKit the default Apps host"
```
