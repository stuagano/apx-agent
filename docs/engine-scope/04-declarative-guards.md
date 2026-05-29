# 04 — Declarative config for built-in guards

**Status:** Scope / design (no implementation)
**Author:** engine scoping pass
**Scope size:** S–M (see [Effort](#9-effort-estimate))

## 0. Goal

Let the **data** part of guardrails be declared in `pyproject.toml` and
auto-applied to the served agent, so a config-driven consumer ("Coworker")
can ship guardrails without writing Python. Custom guard *functions* stay
code (passed via the `LlmAgent` constructor as they are today).

Target config shape (final schema in §3):

```toml
[tool.apx.agent.guardrails]
blocked_tools      = ["delete_account", "issue_refund"]
allowed_tools      = ["classify_intent", "get_recent_orders"]
rate_limit         = 60          # calls/min
injection_detection = true
```

Coworker's own spec uses `guardrails: { blocked_actions: [...] }`; the
mapping from `blocked_actions` → `blocked_tools` is Coworker's adapter
concern, not the engine's. The engine exposes `blocked_tools` /
`allowed_tools` and Coworker's deploy step renames its key.

---

## 1. Inventory — what is data-configurable vs code-only

Source of truth: `python/src/apx_agent/_guards.py` (read in full) and
`python/src/apx_agent/_watchdog.py`.

### 1a. Pure-DATA-configurable built-ins (in scope)

| Config intent | Backing built-in | File:line | Hook it plugs into | Constructor inputs that are pure data |
|---|---|---|---|---|
| Tool allowlist | `ToolAllowlist(names, *, message=None)` | `_guards.py:233` | `before_tool` | `names: Iterable[str]` ✅, `message` ✅ |
| Tool denylist | `ToolDenylist(names, *, message=None)` | `_guards.py:251` | `before_tool` | `names: Iterable[str]` ✅, `message` ✅ |
| Rate limit | `RateLimit(*, per_minute, burst=None, principal_key=None, message=None, max_principals, idle_ttl)` | `_guards.py:50` | `before_tool` | `per_minute` ✅, `burst` ✅, `message` ✅, `max_principals` ✅, `idle_ttl` ✅ — **`principal_key` is a callable → code-only** |
| Injection detection | `prompt_injection_heuristic(*, patterns=None, message=...)` | `_guards.py:168` | `input_guardrails` | on/off ✅, `message` ✅ — **`patterns` (compiled regex) → code-only, the default set is fine for config** |

All four are constructed from primitive scalars/lists and need no live
objects. They are already exported from `__init__.py` (`_guards` import at
`__init__.py:183`).

### 1b. Code-only built-ins (explicitly out of scope for config)

| Built-in | File:line | Why it can't be pure config |
|---|---|---|
| `FeatureFlagGuard(*, provider, gates, message)` | `_guards.py:270` | `provider` is a callable `(flag_name) -> bool` bound to a live flag client (env/LaunchDarkly/Statsig). The `gates` dict *is* data, but the guard is useless without the provider. Could get a config-driven env-var provider later (see [Open questions](#10-open-questions) Q3). |
| `RateLimit(principal_key=...)` | `_guards.py:83` | `principal_key` extracts a principal from tool args — a closure. Config gets a single global bucket only (`principal_key=None`). Per-principal limiting stays code. |
| `prompt_injection_heuristic(patterns=...)` | `_guards.py:170` | Custom `re.Pattern` lists aren't TOML-expressible safely. Config toggles the *default* set on/off. (A future `injection_patterns = ["..."]` extension is possible — Q4.) |
| `compose(...)` | `_guards.py:346` | Pure plumbing, not a guard. The builder uses it internally; not exposed as config. |

### 1c. Watchdog (`_watchdog.py`) — out of scope

`WatchdogGuard` / `WatchdogClient` / `make_watchdog_transport`
(`_watchdog.py:294`, `:188`, `:846`) are the *slow-loop* policy layer.
They require a live `transport` callable (MCP endpoint + UC table +
`WorkspaceClient`). None of that is pure data — the URLs are data but the
SDK clients and the MCP/SQL wiring are not. The module's own docstring
(`_guards.py:5-12`) frames the two layers as deliberately separate:
local zero-latency guards here, Watchdog as a separate posture engine.

**Decision:** Watchdog is **not** in scope for declarative config in this
pass. See [§6 Watchdog](#6-watchdog) for the rationale and the future hook.

---

## 2. Where the guards must actually run (mechanism analysis)

This is the load-bearing detail. Guards split across **two different
runtime surfaces**, and tool deny/allow specifically does **not** belong in
`input_guardrails`.

### 2a. `input_guardrails` / `output_guardrails` — message-text layer

- Stored on `LlmAgent._input_guardrails` / `_output_guardrails`
  (`_agents.py:120-121`).
- Applied in `_apply_input_guardrails` (`_agents.py:131`) and
  `_apply_output_guardrails` (`_agents.py:138`), called from `run()`
  (`_agents.py:148`, `:153`) and `stream()` (`_agents.py:160`, `:174`).
- Signature: `(messages) -> str | None` (input) / `(text) -> str | None`
  (output). Returning a string short-circuits.
- **`prompt_injection_heuristic` belongs here** — it scans message text
  (`_guards.py:193` `_check(messages)`).

### 2b. `before_tool` — tool-call gate (NOT message text)

- Stored on `LlmAgent._before_tool` (`_agents.py:116`).
- Fires through the **LangChain callback handler**, not through
  `run()`/`stream()`. `build_callback_handler(agent)` reads
  `agent._before_tool` (`_callbacks.py:283`) and attaches the handler in
  the compile path (`_compile.py:267-272`,
  `config["callbacks"] = [handler]`).
- The actual gate is `_AgentCallbackHandler.on_tool_start`
  (`_callbacks.py:191`): it calls `_run_hook(self._before_tool, tool_name,
  inputs)` (`_callbacks.py:215`) and **lets the exception propagate**
  (`_callbacks.py:216-218`), which LangChain surfaces as a tool failure and
  aborts the call.
- Signature: `(tool_name, arguments) -> None`, **raise to block**
  (`PermissionError`).

**Conclusion for tool allow/deny:** `ToolAllowlist` / `ToolDenylist` /
`RateLimit` must be attached to **`before_tool`**, never to
`input_guardrails`. Guardrails run on messages/text and have no tool name
to inspect; putting a denylist there would be a no-op. The `before_tool`
callback path is the only place a tool call can actually be intercepted and
aborted at runtime.

Note: only a **single** `before_tool` hook field exists (it's one callable,
not a list). Multiple config guards on this surface must be merged with
`compose(...)` (`_guards.py:346`), which chains callables and short-circuits
on the first raise. Compile-time tool filtering (dropping tools before they
reach the LLM) is an alternative for allowlists but is rejected — see
[§5](#5-tool-allowdeny-specifics).

### 2c. Why mutate the agent instance (timing)

The agent object passed to `setup_agent` is the **same instance** later
wrapped at predict time:

- `create_app` lifespan calls `setup_agent(...)` (`_wiring.py:476`) **then**
  `mount_invocations_route(app, agent, ...)` (`_wiring.py:486`).
- `mount_invocations_route` → `chat_agent_for(agent, ...)`
  (`_invocations.py:104`); compile reads `agent._before_tool` /
  `agent._input_guardrails` **at predict time**, not at construction.

So appending to `agent._input_guardrails` / setting `agent._before_tool`
inside `setup_agent` is observed by every subsequent request. This is the
correct, lowest-blast-radius integration point.

---

## 3. Config schema

### 3a. Nesting decision

Use a **dedicated sub-table**: `[tool.apx.agent.guardrails]`.

Rationale:
- `_load_agent_config` (`_inspection.py:127`) filters the `[tool.apx.agent]`
  section to keys in `AgentConfig.model_fields` (`_inspection.py:179`).
  A nested table value (`guardrails = {...}`) is a single key `guardrails`,
  so adding **one** field `guardrails` to `AgentConfig` is enough; the inner
  keys ride along as a sub-model.
- Keeps the flat `[tool.apx.agent]` namespace clean (it already holds
  `name`, `model`, `instructions`, `sub_agents`, etc. — `_models.py:57-71`).
- Inline-table form `guardrails = { blocked_tools = [...] }` and the
  `[tool.apx.agent.guardrails]` header form are equivalent in TOML; document
  the header form for readability.

### 3b. Schema (new pydantic model)

Add to `_models.py`:

```python
class GuardrailsConfig(BaseModel):
    """Data-only declaration of built-in guards. See _guards.py."""
    model_config = {"extra": "forbid"}   # unknown keys → validation error (§7)

    allowed_tools: list[str] | None = None   # ToolAllowlist; None = no allowlist
    blocked_tools: list[str] = []            # ToolDenylist
    rate_limit: int | None = None            # RateLimit(per_minute=...); None = off
    rate_limit_burst: int | None = None      # RateLimit(burst=...); None = per_minute
    injection_detection: bool = False        # prompt_injection_heuristic()
```

And one field on `AgentConfig` (`_models.py:57`):

```python
    guardrails: GuardrailsConfig = GuardrailsConfig()
```

Because `guardrails` is now in `AgentConfig.model_fields`, the
`_inspection.py:179` filter passes the sub-table through automatically. No
change to the loader's filtering logic is required.

### 3c. Key → constructor mapping

| Config key | Builds | Surface |
|---|---|---|
| `allowed_tools` | `ToolAllowlist(allowed_tools)` (`_guards.py:233`) | `before_tool` |
| `blocked_tools` | `ToolDenylist(blocked_tools)` (`_guards.py:251`) | `before_tool` |
| `rate_limit` (+ optional `rate_limit_burst`) | `RateLimit(per_minute=rate_limit, burst=rate_limit_burst)` (`_guards.py:50`) | `before_tool` |
| `injection_detection = true` | `prompt_injection_heuristic()` (`_guards.py:168`) | `input_guardrails` |

Defaults that stay implicit (not config-exposed in v1): `RateLimit`'s
`max_principals=10_000`, `idle_ttl=3600`, `principal_key=None` (global
bucket), and the injection default pattern set.

### 3d. Full example

```toml
[tool.apx.agent]
name = "customer_triage"
model = "databricks-claude-sonnet-4-6"

[tool.apx.agent.guardrails]
allowed_tools       = ["classify_intent", "get_recent_orders"]
blocked_tools       = ["delete_account"]
rate_limit          = 60
rate_limit_burst    = 10
injection_detection = true
```

---

## 4. Builder + attachment

### 4a. New function — `build_config_guards`

Add to `_guards.py` (keeps all guard knowledge in one module; avoids a new
file):

```python
def build_config_guards(cfg: "GuardrailsConfig") -> tuple[
    list[Callable[..., Any]],          # input guardrails to append
    Callable[[str, dict], None] | None # before_tool gate (or None)
]:
    """Translate a GuardrailsConfig into built-in guard callables.

    Returns (input_guardrails, before_tool_gate). before_tool_gate is a
    single composed callable (or None) so the caller can merge it with any
    code-defined before_tool hook.
    """
    input_guards: list[Callable[..., Any]] = []
    tool_gates: list[Callable[[str, dict], None]] = []

    if cfg.injection_detection:
        input_guards.append(prompt_injection_heuristic())

    if cfg.allowed_tools is not None:
        tool_gates.append(ToolAllowlist(cfg.allowed_tools))
    if cfg.blocked_tools:
        tool_gates.append(ToolDenylist(cfg.blocked_tools))
    if cfg.rate_limit is not None:
        tool_gates.append(RateLimit(per_minute=cfg.rate_limit,
                                    burst=cfg.rate_limit_burst))

    before_tool = compose(*tool_gates) if tool_gates else None
    return input_guards, before_tool
```

Note: `compose` (`_guards.py:346`) short-circuits on the first raise, so the
gate order is deterministic: allowlist → denylist → rate limit. (Order
chosen so a not-allowed tool is rejected before consuming a rate-limit
token. If both allow- and deny-list name a tool, allow-then-deny means a
denied tool already excluded from the allowlist fails the allowlist first;
a tool present in both lists is denied. Document this; it's an unusual
config but should be defined behavior.)

### 4b. Attachment in `setup_agent`

Integration point: `setup_agent` in `_wiring.py`, right after the existing
sub-agent merge block (after `_wiring.py:110`, before `tools =
agent.collect_tools()` at `_wiring.py:112`). The agent is still the live
instance; hooks read at predict time (§2c).

```python
    # --- declarative guardrails (config data -> built-in guards) ---
    from ._guards import build_config_guards, compose

    input_guards, before_tool_gate = build_config_guards(config.guardrails)

    if input_guards:
        existing = getattr(agent, "_input_guardrails", None)
        if existing is None:
            logger.warning(
                "config guardrails.injection_detection set on a %s root, "
                "which has no _input_guardrails (only LlmAgent does) — ignored.",
                type(agent).__name__,
            )
        else:
            # ADDITIVE: code-defined guards run first, then config guards.
            existing.extend(input_guards)

    if before_tool_gate is not None:
        if not hasattr(agent, "_before_tool"):
            logger.warning(
                "config guardrails tool rules set on a %s root, which has no "
                "_before_tool (only LlmAgent does) — ignored.",
                type(agent).__name__,
            )
        else:
            code_hook = getattr(agent, "_before_tool", None)
            # ADDITIVE: code hook runs first, then the config gate.
            agent._before_tool = (
                compose(code_hook, before_tool_gate) if code_hook
                else before_tool_gate
            )
```

This mirrors the existing `sub_agents` merge, which already special-cases
"only `LlmAgent` defines the attribute" and warns loudly rather than failing
silently (`_wiring.py:87-98`). Use the identical pattern.

### 4c. Composition semantics (config vs code-defined guards)

- **Additive, never replacing.** Code-defined guards passed to the
  `LlmAgent` constructor are preserved; config guards are appended.
- **Order — input guardrails:** code first, then config (so a hand-written
  guard that, e.g., normalizes messages runs before the injection check).
  `_apply_input_guardrails` (`_agents.py:131`) returns on the first
  non-`None`, so order is the rejection precedence.
- **Order — `before_tool`:** code hook first, then config gate, merged via
  `compose` (`_guards.py:346`). First raise wins.
- **Idempotency:** `setup_agent` can run more than once in some mount paths
  (e.g. `mount_mcp_endpoints` startup at `_wiring.py:582`). Appending guards
  on every call would double them. **Guard against re-entry** with a
  sentinel attribute (e.g. `agent._apx_config_guards_applied = True`) checked
  at the top of the block. This is a real correctness requirement, not a
  nicety — `mount_mcp_endpoints` and `create_app` can both fire `setup_agent`
  on the same instance.

---

## 5. Tool allow/deny specifics

Already established in §2b. Restating the decision crisply:

- **Mechanism: `before_tool` callback gate.** `ToolAllowlist` /
  `ToolDenylist` raise `PermissionError` in `on_tool_start`
  (`_callbacks.py:191-218`); LangChain aborts the tool call and surfaces the
  message to the LLM as a tool failure. This blocks **execution**, which is
  what "blocked_actions" must mean.

- **Rejected alternative — compile-time tool filtering.** We could drop
  disallowed tools from `agent._tool_fns` before compile so the LLM never
  sees them. Rejected because:
  1. It changes the tool *schema* the model is given (the model can't even
     attempt the tool), which is a different, less observable behavior than a
     runtime block. Coworker's `blocked_actions` semantics expect a *block*,
     not *invisibility*.
  2. A denylist with compile-time filtering and an allowlist need different
     code paths; the `before_tool` gate handles both uniformly.
  3. `before_tool` produces an auditable rejection on the span
     (`on_tool_start` already sets `operation="tool_call"`,
     `tool_name=...` audit attrs at `_callbacks.py:205-211`); a filtered-out
     tool produces no trace.

  (If a future requirement is "hide the tool from the model entirely,"
  add a separate `hidden_tools` key with compile-time filtering — out of
  scope here.)

- **`input_guardrails` is the wrong surface** for tool rules: it sees
  `messages`, not tool calls (`_guards.py:193`), so a denylist there is a
  no-op. This is the single most important correctness point in the doc.

---

## 6. Watchdog

**Out of scope for declarative config in this pass.**

- `WatchdogGuard` (`_watchdog.py:294`) needs a live `WatchdogClient`
  (`_watchdog.py:188`) with a `transport` callable. The canonical transport
  `make_watchdog_transport` (`_watchdog.py:846`) requires an MCP URL **and**
  a UC violations table **and** a `WorkspaceClient` — none pure data, and the
  default `_noop_transport` (`_watchdog.py:174`) would make config-declared
  Watchdog a silent no-op, which is worse than not offering it.
- The module is deliberately the *slow-loop posture* layer, distinct from
  the in-process guards (`_guards.py:5-12`).

**Future hook (documented, not built):** a `[tool.apx.agent.watchdog]`
sub-table with `mcp_url`, `mcp_tool_name`, `violations_table`,
`warehouse_id` could let `setup_agent` build a transport using the app's
`app.state.workspace_client` (`_wiring.py:473`) and attach
`WatchdogGuard(...).for_input()/.for_tool()`. That's a separate scope item
(it crosses into live SDK wiring and OBO concerns) — track as a follow-on,
not part of "declarative built-in guards."

---

## 7. Error handling

Validation happens at config-load time (pydantic) and at builder time.

| Failure | Where caught | Behavior |
|---|---|---|
| Unknown guardrails key (e.g. `rate_limt = 60`) | `GuardrailsConfig` with `model_config = {"extra": "forbid"}` | Pydantic `ValidationError` at `AgentConfig(**...)` (`_inspection.py:179`). **Fail loud at startup** — a typo'd guard silently doing nothing is the dangerous case. |
| Bad threshold (`rate_limit = 0` / negative) | `RateLimit.__init__` already raises `ValueError` for `per_minute <= 0` (`_guards.py:88`). Surfaces from `build_config_guards`. | Startup error with the constructor's message. Optionally pre-validate in `GuardrailsConfig` with `field_validator` for a friendlier message. |
| Wrong type (`blocked_tools = "delete"` instead of a list) | Pydantic field typing on `GuardrailsConfig` | `ValidationError` at load. |
| Guardrails on a non-`LlmAgent` root (Sequential/Parallel) | `setup_agent` attachment block (§4b) | **Warn and skip** (matches the `sub_agents` precedent at `_wiring.py:87-98`). Don't crash — the composition root legitimately has no guard surface. |
| Empty config (`[tool.apx.agent.guardrails]` absent) | Default `GuardrailsConfig()` | No-op; `build_config_guards` returns `([], None)`. Zero behavior change for existing agents. |

Loud-at-startup over silent-no-op is the governing principle: a guard that
silently fails to apply is a security regression.

One caveat on `extra="forbid"`: `_read_apx_agent_config` (the CLI-side
reader at `cli.py:78`) returns the raw dict and does **not** construct
`AgentConfig`, so it won't enforce `forbid`. The enforcement path that
matters is `_load_agent_config` → `AgentConfig(**...)` (`_inspection.py:179`),
which runs in `setup_agent`. That's the served path, so it's covered.
`apx doctor` could additionally validate (Q5).

---

## 8. Testing plan

New tests (extend `python/tests/test_guards.py` and `test_wiring.py`):

**Builder unit tests** (`test_guards.py`):
1. `build_config_guards(GuardrailsConfig())` → `([], None)`.
2. `injection_detection=True` → one input guard that flags a known pattern
   (reuse the existing `prompt_injection_heuristic` test corpus).
3. `blocked_tools=["x"]` → `before_tool` gate raises `PermissionError` for
   `"x"`, passes for `"y"`.
4. `allowed_tools=["x"]` → gate passes `"x"`, raises for `"y"`.
5. `rate_limit=2` → third call within the window raises `PermissionError`.
6. allow + deny + rate together → composed order; denied tool raises first.

**Schema/validation tests** (`test_wiring.py` or a new `test_config.py`):
7. `[tool.apx.agent.guardrails]` parses into `AgentConfig.guardrails`
   (round-trip a temp `pyproject.toml` through `_load_agent_config`).
8. Unknown key → `ValidationError`.
9. `rate_limit = 0` → `ValidationError` / `ValueError` at build.

**Attachment / integration tests** (`test_wiring.py`):
10. `setup_agent` with config guards appends to `agent._input_guardrails`
    and sets `agent._before_tool` on an `LlmAgent`.
11. **Additive**: a code-defined input guard + config injection guard → both
    present, code first.
12. **Additive `before_tool`**: a code `before_tool` + config gate → both
    fire (composed); code first.
13. **Idempotency**: calling `setup_agent` twice does not double the guards
    (sentinel check).
14. Guards declared on a `SequentialAgent`/`ParallelAgent` root → warning
    logged, no crash, no attributes mutated.
15. End-to-end (if the langgraph extra is available in CI): an `LlmAgent`
    served via `create_app` with `blocked_tools` actually aborts a call to
    the blocked tool (assert the rejection surfaces). Mark `@pytest.mark`
    skip-if-no-langgraph to match existing compile-path test gating.

---

## 9. Effort estimate

**S–M.** All the guards exist and are exported; this is wiring + config
plumbing, no new guard logic.

| Work item | Files | Rough LOC |
|---|---|---|
| `GuardrailsConfig` model + `AgentConfig.guardrails` field | `_models.py` | ~20 |
| `build_config_guards` | `_guards.py` | ~30 |
| `setup_agent` attachment block + idempotency sentinel | `_wiring.py` | ~30 |
| Tests | `test_guards.py`, `test_wiring.py` (+ maybe `test_config.py`) | ~150 |
| Docs (config reference, scaffold comment) | docs / scaffold `pyproject.toml` | ~30 |
| **Total** | | **~260 LOC**, ~120 of it production |

No new dependencies. No change to the compile path or callback handler.

---

## 10. Open questions

- **Q1.** Coworker's key is `blocked_actions`; engine key is `blocked_tools`.
  Confirm the adapter (rename) lives in Coworker's deploy step, not the
  engine. Recommendation: engine stays tool-centric; do **not** add a
  `blocked_actions` alias (would invite drift). If an alias is required,
  add it as a pydantic validation alias on `GuardrailsConfig`, not a second
  field.
- **Q2.** `allowed_tools` + `blocked_tools` both set — is "allow-list wins,
  then deny within it" the intended precedence? §4a defines a behavior;
  confirm it matches Coworker's mental model, or forbid both being set at
  once via a model validator.
- **Q3.** Do we want a config-driven `FeatureFlagGuard` using an env-var
  provider (e.g. `feature_flags = { premium_search = "PREMIUM_V2" }` →
  `provider=env_flag`)? Cleanly possible but adds a provider convention.
  Deferred.
- **Q4.** Should `injection_detection` accept extra patterns
  (`injection_patterns = ["regex", ...]`)? TOML-expressible but raises
  regex-injection/ReDoS validation concerns. Deferred; default set only in
  v1.
- **Q5.** Should `apx doctor` validate `[tool.apx.agent.guardrails]` (it
  currently reads raw config via `_read_apx_agent_config`, `cli.py:78`,
  which bypasses pydantic `forbid`)? Low effort, good ergonomics — recommend
  yes as a fast-follow.
- **Q6.** Rate limit scope: config gives a single **global** bucket
  (`principal_key=None`). Is per-user limiting needed at config level? That
  needs a principal extractor (a callable) — would require a named
  convention like `rate_limit_by = "user_id"` mapping to a built-in
  arg-extractor. Deferred; document the global-only limitation.
```
