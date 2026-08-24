# Service Policies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a portable Service Policy declaration to apx-agent, mirror it through existing local governance hooks, and emit/apply/verify native Databricks service-policy plans without silently weakening enforcement.

**Architecture:** `ServicePoliciesConfig` is the canonical Pydantic model attached to `AgentConfig`. A local adapter projects it into existing `PolicyGate`, `PromptPolicy`, `WatchdogGuard`, and lifecycle hooks. A separate native adapter converts the same validated declaration into a deterministic plan and uses a verified Databricks transport for explicit apply and read-after-write verification. ABAC selectors are represented as desired state and reported as unsupported until the native attachment surface supports them.

**Tech Stack:** Python 3.11+, Pydantic, existing apx-agent lifecycle hooks, pytest, Databricks SDK/REST surface verified during Task 1, TOML/YAML serialization, TypeScript after the Python contract is stable.

**Spec:** `docs/superpowers/specs/2026-08-24-service-policies-prd.md`

## Global Constraints

- Preserve existing `[tool.apx.agent.guardrails]`, code-defined hooks, `PolicyGate`, and `WatchdogGuard` behavior.
- Reuse existing policy, approval, audit, config, YAML, project-generation, and deployment seams before adding new abstractions.
- `native_mode = "plan"` is side-effect-free; `apply` and `required` need an explicitly supplied Databricks profile/target and must not use the ambient profile implicitly.
- Enforce mode fails closed on evaluator, capability, permission, malformed-response, and verification failures; dry-run records failures without blocking.
- Native `ASK` is valid only for MCP request/`ON CALL` policy evaluation unless a verified platform capability says otherwise.
- Input policies run in ascending rank; output policies run in reverse rank; the first `DENY` stops the phase.
- Do not add a dependency until the existing installed Databricks SDK/HTTP helpers have been checked and found insufficient.
- Do not mutate a Databricks workspace during tests or plan generation; live apply verification is an explicit operator action using `--profile <name>`.
- Do not expose credentials, raw sensitive payloads, full classifier prompts, or SQL secrets in logs, plans, receipts, or errors.
- Keep unrelated dirty files untouched: `python/uv.lock`, `.agents/`, `.codex/`, `.relentless_logs/`, `_internal/`, and the existing PRD/worktree artifacts.
- Run `git diff --check` and the narrowest relevant pytest command after each task; run `make check` before claiming the implementation is complete.

## File Map

Create:

- `python/src/apx_agent/_service_policies.py` — canonical enums, Pydantic declaration models, phase/rank ordering, events, decisions, and validation.
- `python/src/apx_agent/_service_policies_local.py` — local evaluator registry and hook adapters.
- `python/src/apx_agent/_service_policies_native.py` — native capability model, deterministic plan, transport boundary, apply, and verification.
- `python/tests/test_service_policies.py` — canonical model and ordered-decision tests.
- `python/tests/test_service_policies_local.py` — local adapter and hook behavior tests.
- `python/tests/test_service_policies_native.py` — native plan/apply/verification tests using fake transports only.
- `python/tests/test_service_policies_wiring.py` — real agent-leaf wiring/reality tests.
- `docs/reference/service-policies.md` — user-facing declaration, capability matrix, local/native differences, and migration examples.

Modify:

- `python/src/apx_agent/_models.py` — add `ServicePoliciesConfig` to `AgentConfig` without changing existing defaults.
- `python/src/apx_agent/_wiring.py` — apply the local projection through the shared config-to-instance seam used by both run and deploy.
- `python/src/apx_agent/_policy.py` — add the ordered short-circuit evaluator seam while preserving the existing max-action `evaluate_policies` behavior.
- `python/src/apx_agent/_project_gen.py` — serialize the declaration into generated `pyproject.toml`.
- `python/src/apx_agent/__init__.py` — export only the stable public policy types and plan/decision entry points.
- `python/src/apx_agent/cli.py` — expose policy information in `agents describe` and add a side-effect-free native plan command or plan output path consistent with existing CLI conventions.
- `python/tests/test_wiring.py`, `python/tests/test_policy.py`, `python/tests/test_cli_scaffold_yaml.py`, and relevant project-generation tests — add regression coverage without weakening existing assertions.
- `docs/reference/configuration.md`, `docs/reference/pyproject-toml.md`, and `docs/safety/compliance.md` — document the new declaration and distinguish local mirror behavior from native enforcement.
- `typescript/src/service-policies.ts`, `typescript/src/index.ts`, and `typescript/tests/service-policies.test.ts` — add parity only after the Python contract and compatibility fixtures are stable.

---

### Task 1: Verify the native Databricks surface and freeze capability fixtures

**Files:**

- Create: `docs/reference/service-policies-native-capabilities.md`
- Create: `python/tests/fixtures/service_policy_capabilities.json`
- Test: `python/tests/test_service_policies_native.py`

**Interfaces:**

- Consumes: official Databricks Service Policy documentation and the installed `databricks-sdk`/HTTP helpers.
- Produces: a checked-in capability fixture and a documented decision about which native operations are available for plan, apply, and verification.

- [ ] **Step 1: Inspect the installed SDK and repository helpers without external writes**

Run:

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
uv run python -c "import databricks.sdk; print(databricks.sdk.__file__)"
rg -n -i "service.?polic|ai.?gateway|unity.?catalog.*polic|mcp.?service" python/src python/.venv 2>/dev/null | head -200
~~~

Expected: a concrete inventory of existing client methods/helpers, or evidence that no supported service-policy attachment client exists.

- [ ] **Step 2: Record the native contract and capability boundary**

Write `docs/reference/service-policies-native-capabilities.md` with these sections:

~~~markdown
# Native Service Policy Capabilities

## Verified now
## Plan-only operations
## Apply operations
## Verification operations
## Unsupported beta behavior
## Required permissions and profile inputs
~~~

Use the official Service Policy links from the PRD. Record that current beta attachment is individual-service scoped and that ABAC selectors are desired state until an attachment API is verified.

- [ ] **Step 3: Freeze a transport-neutral capability fixture**

Create `python/tests/fixtures/service_policy_capabilities.json` with a versioned structure containing target types, supported kinds/phases, supported ASK phases, ABAC support, apply support, and verification support. Keep it independent of a workspace profile and free of credentials.

- [ ] **Step 4: Add the fixture read test**

Load the fixture, validate that every target has non-empty `kinds` and `phases` lists, and assert that the fixture’s ABAC/apply values match the documented capability record.

Run:

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/python
uv run pytest tests/test_service_policies_native.py -q
~~~

Expected: PASS with no workspace access.

- [ ] **Step 5: Commit the inventory**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add -f docs/reference/service-policies-native-capabilities.md python/tests/fixtures/service_policy_capabilities.json python/tests/test_service_policies_native.py
git commit -m "docs: inventory native service policy capabilities"
~~~

### Task 2: Add the canonical Service Policy declaration and ordered decision model

**Files:**

- Create: `python/src/apx_agent/_service_policies.py`
- Create: `python/tests/test_service_policies.py`
- Modify: `python/src/apx_agent/_models.py:301-323`

**Interfaces:**

- Consumes: `AgentConfig`, Pydantic `BaseModel`, existing `PolicyEvent`, `PolicyResult`, and `PolicyAction` concepts.
- Produces: `ServicePoliciesConfig`, `ServicePolicyAttachment`, `ServicePolicy`, `ServicePolicyEvent`, `ServicePolicyDecision`, and deterministic policy ordering used by both adapters.

- [ ] **Step 1: Write validation tests first**

Add tests for a valid declaration, unknown fields, missing classifier/prompt/function, invalid target, invalid mode, negative/non-integer rank, invalid built-in phase, and ABAC selector serialization:

~~~python
def test_valid_service_policy_declaration() -> None:
    config = ServicePoliciesConfig.model_validate({
        "local_mode": "mirror",
        "native_mode": "plan",
        "attachments": [{
            "name": "mcp-guardrails",
            "target_type": "mcp_service",
            "target": "main.tools.github",
            "mode": "enforce",
            "policies": [{
                "name": "delete-approval",
                "kind": "llm_judge",
                "classifier": "databricks-claude-haiku-4-5",
                "prompt": "Require approval for destructive writes.",
                "phase": "on_call",
                "rank": 100,
            }],
        }],
    })
    assert config.attachments[0].policies[0].rank == 100


def test_unknown_fields_fail_closed() -> None:
    with pytest.raises(ValidationError):
        ServicePoliciesConfig.model_validate({"unexpected": True})
~~~

- [ ] **Step 2: Implement strict enums and Pydantic models**

Implement `ServicePolicyTargetType`, `ServicePolicyKind`, `ServicePolicyPhase`, `ServicePolicyMode`, and `NativePolicyMode` as strict string enums. Add `BuiltinServicePolicy`, `ServicePolicy`, `ServicePolicyTarget`, `ServicePolicyAttachment`, `AbacSelector`, and `ServicePoliciesConfig` with `ConfigDict(extra="forbid")`.

Use validators to enforce:

- built-in identifiers are `sensitive_data`, `unsafe_content`, `jailbreak`, or `hallucination`;
- jailbreak is request-only and hallucination is result-only;
- LLM judge requires classifier and prompt;
- SQL requires a UC function reference;
- rank is a finite non-negative integer;
- `ASK` is only allowed for MCP `on_call`;
- `native_mode = "required"` cannot coexist with an empty target;
- ABAC selectors serialize as desired state but are not treated as current native support.

Keep values serializable with `model_dump(mode="json", exclude_none=True)`.

- [ ] **Step 3: Add event, decision, and capability error types**

Define:

~~~python
@dataclass(frozen=True)
class ServicePolicyEvent:
    phase: str
    target_type: ServicePolicyTargetType
    target: str
    content: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: Any = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServicePolicyDecision:
    action: Literal["ALLOW", "DENY", "ASK", "UNAVAILABLE"]
    reason: str | None
    policy_name: str | None
    phase: str
    rank: int | None
    mode: ServicePolicyMode
    adapter: Literal["local", "native"]
~~~

Add `ServicePolicyCapabilityError` and `ServicePolicyEvaluationError` with target/policy/phase context.

- [ ] **Step 4: Implement ordering and first-DENY evaluation**

Add:

~~~python
def ordered_policies(
    policies: Sequence[ServicePolicy],
    phase: ServicePolicyPhase,
) -> tuple[ServicePolicy, ...]:
    ...


def evaluate_ordered_service_policies(
    policies: Sequence[ServicePolicy],
    event: ServicePolicyEvent,
    *,
    mode: ServicePolicyMode,
    adapter: Literal["local", "native"],
) -> ServicePolicyDecision:
    """Evaluate in supplied rank order; stop at DENY or first ASK."""
~~~

Use ascending rank for `on_call` and descending rank for `on_result`. In enforce mode, evaluator failures never become `ALLOW`. In dry-run mode, record `DENY`/`ASK`/failure without blocking.

- [ ] **Step 5: Attach the model to AgentConfig**

Add `service_policies: ServicePoliciesConfig = Field(default_factory=ServicePoliciesConfig)` to `AgentConfig`. Keep the default empty and prove `AgentConfig(name="x")` remains backward-compatible.

- [ ] **Step 6: Run the focused contract tests**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/python
uv run pytest tests/test_service_policies.py tests/test_wiring.py::TestGuardrailsConfig -q
~~~

- [ ] **Step 7: Commit the canonical model**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_service_policies.py python/src/apx_agent/_models.py python/tests/test_service_policies.py
git commit -m "feat: add canonical service policy models"
~~~

### Task 3: Build the local mirror adapter

**Files:**

- Create: `python/src/apx_agent/_service_policies_local.py`
- Create: `python/tests/test_service_policies_local.py`
- Modify: `python/src/apx_agent/_policy.py:293-327,590-700`

**Interfaces:**

- Consumes: `ServicePoliciesConfig`, `ServicePolicyEvent`, `ServicePolicyDecision`, `PolicyGate`, `ApprovalStore`, `PromptPolicy`, `prompt_injection_heuristic`, and optional `WatchdogClient`.
- Produces: `LocalServicePolicyAdapter` with `for_input()`, `for_output()`, `for_tool()`, `for_model()`, and `for_tool_result()` hook factories.

- [ ] **Step 1: Add ordered-evaluator regression tests before changing _policy.py**

Cover ascending input order, descending output order, first-DENY short-circuit, dry-run non-blocking behavior, approval creation/consumption, approval fingerprint mismatch, classifier parse failure, evaluator exception, and SQL-without-local-evaluator behavior.

- [ ] **Step 2: Add an evaluator seam to PolicyGate without changing default behavior**

Extend `PolicyGate.__init__` with an optional keyword-only evaluator:

~~~python
def __init__(
    self,
    policies: Sequence[Any],
    *,
    approval_store: ApprovalStore | None = None,
    context: dict[str, Any] | None = None,
    evaluator: Callable[[Sequence[Any], PolicyEvent], PolicyResult] = evaluate_policies,
) -> None:
    ...
~~~

Use the supplied evaluator only at the existing evaluation call site. Existing callers continue to use `evaluate_policies`, so current max-action behavior remains unchanged. Add a regression test proving the default evaluator still evaluates all policies and the injected evaluator can short-circuit.

- [ ] **Step 3: Implement local evaluator mappings**

Implement `build_local_policy_evaluators(config, *, watchdog=None, local_evaluators=None)`:

- `builtin:jailbreak` reuses `prompt_injection_heuristic` for request content.
- `llm_judge` reuses `PromptPolicy` with the declared classifier and prompt.
- `builtin:sensitive_data`, `builtin:unsafe_content`, and `builtin:hallucination` use an injected evaluator or configured Watchdog transport; without one, enforce mode returns a fail-closed `DENY` and dry-run returns `UNAVAILABLE` with a diagnostic reason.
- `sql` has no Python SQL interpreter; without an injected evaluator it follows the same enforce/dry-run fail-closed rule.

Do not log the full classifier prompt, raw request content, or SQL function arguments.

- [ ] **Step 4: Implement hook adapters**

Implement:

~~~python
class LocalServicePolicyAdapter:
    def for_input(self) -> Callable[[list[Any]], str | None]: ...
    def for_output(self) -> Callable[[str], str | None]: ...
    def for_tool(self) -> Callable[[str, dict[str, Any]], None]: ...
    def for_model(self) -> Callable[[list[Any]], None]: ...
    def for_tool_result(self) -> Callable[[str, dict[str, Any], Any], None]: ...
~~~

Use the existing `PolicyGate` approval store for `ASK` on local tool calls. Return the existing guardrail string shape for input/output denial. Preserve existing Watchdog redaction behavior where its hook supports rewriting. Treat local `ASK` on output as a validation error rather than inventing a new approval surface.

- [ ] **Step 5: Run local adapter tests and existing policy tests**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/python
uv run pytest tests/test_service_policies_local.py tests/test_policy.py tests/test_watchdog.py -q
~~~

- [ ] **Step 6: Commit the local adapter**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_service_policies_local.py python/src/apx_agent/_policy.py python/tests/test_service_policies_local.py python/tests/test_policy.py
git commit -m "feat: mirror service policies through local guards"
~~~

### Task 4: Wire configuration through every runtime and generated-project path

**Files:**

- Modify: `python/src/apx_agent/_wiring.py:43-285` and the `finalize_agent` call site.
- Modify: `python/src/apx_agent/_project_gen.py:86-190`.
- Modify: `python/src/apx_agent/_yaml_spec.py:84-133`.
- Modify: `python/src/apx_agent/__init__.py:249-265,580-600`.
- Create or modify: `python/tests/test_service_policies_wiring.py`, `python/tests/test_cli_scaffold_yaml.py`, and project-generation tests.

**Interfaces:**

- Consumes: `AgentConfig.service_policies` and `LocalServicePolicyAdapter`.
- Produces: idempotent local policy attachment on every eligible leaf and round-trippable YAML/TOML/generated-project config.

- [ ] **Step 1: Add wiring tests before implementation**

Test leaf attachment, idempotency, composition roots, nested `agent_tool` specialists, remote-agent leaves, empty config, no eligible leaf, and code-defined hook precedence:

~~~python
def test_service_policies_attach_to_leaf_agent_once() -> None:
    agent = LlmAgent(name="leaf", tools=[get_weather])
    config = config_with_service_policy()
    apply_config_service_policies(agent, config)
    first = agent._before_tool
    apply_config_service_policies(agent, config)
    assert agent._before_tool is first


def test_service_policies_use_the_shared_config_seam() -> None:
    config = config_with_service_policy()
    agent = LlmAgent(name="leaf", tools=[])
    apply_config_knobs(agent, config)
    assert getattr(agent, "_apx_service_policies_applied", False) is True
~~~

- [ ] **Step 2: Implement apply_config_service_policies**

Add:

~~~python
def apply_config_service_policies(agent: BaseAgent, config: AgentConfig) -> None:
    """Attach local service-policy hooks to every eligible leaf once."""
~~~

Reuse `_collect_guardrail_targets` and the existing idempotent sentinel pattern. Empty config sets the root sentinel and returns. A declared policy with no eligible local leaf raises `ValueError` naming the agent and target.

Call it from the shared finalization path after existing config guardrails so Apps startup and model-serving log capture receive the same local mirror.

- [ ] **Step 3: Serialize service policies in generated pyproject.toml**

Extend `_build_pyproject(config)` to emit the service-policy table, attachment tables, policy tables, and ABAC selector using the existing quoting/order style. Add a generated-project assertion that serialization is parseable and round-trips to the same JSON model dump.

- [ ] **Step 4: Verify YAML loading and strict placeholder behavior**

Add YAML fixtures containing the service-policy block. Assert environment references resolve in target/function/classifier fields and unresolved `$SERVICE_NAME` is rejected in strict mode before deploy.

- [ ] **Step 5: Export stable public types**

Export only canonical models, `ServicePolicyDecision`, `LocalServicePolicyAdapter`, and native plan/receipt types from `apx_agent.__init__`. Keep transport internals private until the native API is stable.

- [ ] **Step 6: Run wiring and generation tests**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/python
uv run pytest tests/test_service_policies_wiring.py tests/test_wiring.py tests/test_cli_scaffold_yaml.py tests/test_yaml_spec.py -q
~~~

- [ ] **Step 7: Commit the config/wiring slice**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_wiring.py python/src/apx_agent/_project_gen.py python/src/apx_agent/_yaml_spec.py python/src/apx_agent/_models.py python/src/apx_agent/__init__.py python/tests/test_service_policies_wiring.py python/tests/test_wiring.py python/tests/test_cli_scaffold_yaml.py
git commit -m "feat: wire declarative service policies"
~~~

### Task 5: Implement native plan, apply, and verification

**Files:**

- Create: `python/src/apx_agent/_service_policies_native.py`.
- Modify: `python/src/apx_agent/cli.py` at existing deploy/describe command seams.
- Modify: `python/tests/test_service_policies_native.py`.
- Modify: `docs/reference/service-policies-native-capabilities.md`.

**Interfaces:**

- Consumes: `ServicePoliciesConfig`, Task 1 capability fixture, explicit target/profile inputs, and the verified native transport surface.
- Produces: `NativePolicyPlan`, `NativePolicyApplyReceipt`, `NativePolicyVerification`, `build_native_policy_plan`, `apply_native_policy_plan`, and `verify_native_policy_plan`.

- [ ] **Step 1: Write pure plan tests first**

Cover deterministic repeated plans, unsupported target/kind/phase, plan mode making no transport call, apply transport failure, malformed native decision, verification timeout/status, and required mode failing when any attachment is unverified:

~~~python
def test_native_plan_is_deterministic_and_side_effect_free() -> None:
    config = config_with_builtin_sql_and_judge()
    first = build_native_policy_plan(config, capabilities=fixture_capabilities())
    second = build_native_policy_plan(config, capabilities=fixture_capabilities())
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.operations


def test_plan_does_not_call_transport() -> None:
    build_native_policy_plan(config_with_service_policy(), capabilities=fixture_capabilities())
    assert transport.operations == []
~~~

- [ ] **Step 2: Define transport-neutral native types**

Implement:

~~~python
@dataclass(frozen=True)
class NativePolicyCapability:
    target_type: ServicePolicyTargetType
    kinds: frozenset[ServicePolicyKind]
    phases: frozenset[ServicePolicyPhase]
    ask_phases: frozenset[ServicePolicyPhase]
    supports_apply: bool
    supports_verify: bool


@dataclass(frozen=True)
class NativePolicyPlan:
    native_mode: NativePolicyMode
    operations: tuple[dict[str, Any], ...]
    unsupported: tuple[str, ...]
    declaration_fingerprint: str


class NativePolicyTransport(Protocol):
    def apply(self, operation: dict[str, Any], *, profile: str) -> dict[str, Any]: ...
    def read(self, operation: dict[str, Any], *, profile: str) -> dict[str, Any]: ...
~~~

Keep operation payloads deterministic and sanitized. Include policy name, target, kind, phase, rank, mode, and references; omit raw prompts and credentials. Hash sensitive declaration content for identity.

- [ ] **Step 3: Implement capability validation and plan generation**

Implement:

~~~python
def build_native_policy_plan(
    config: ServicePoliciesConfig,
    *,
    capabilities: Mapping[ServicePolicyTargetType, NativePolicyCapability],
) -> NativePolicyPlan:
    """Validate and emit a deterministic, side-effect-free native plan."""
~~~

Return unsupported entries in plan mode for operator review. Raise `ServicePolicyCapabilityError` for `native_mode = "required"` and explicit apply requests. Preserve phase/rank ordering in operations.

- [ ] **Step 4: Implement the verified native transport**

Use the exact SDK/REST surface recorded in Task 1. If the installed surface cannot apply service-policy attachments, implement plan-only transport behavior and make `apply_native_policy_plan` raise `ServicePolicyCapabilityError` with the documented reason. Do not invent an undocumented endpoint or add a dependency solely to make apply appear complete.

Implement:

~~~python
def apply_native_policy_plan(
    plan: NativePolicyPlan,
    *,
    transport: NativePolicyTransport,
    profile: str,
) -> NativePolicyApplyReceipt:
    """Apply every supported operation and fail closed on any error."""
~~~

Require a non-empty explicit `profile`. Never read `DATABRICKS_CONFIG_PROFILE` as fallback.

- [ ] **Step 5: Implement read-after-write verification**

Implement:

~~~python
def verify_native_policy_plan(
    plan: NativePolicyPlan,
    *,
    transport: NativePolicyTransport,
    profile: str,
) -> NativePolicyVerification:
    """Read back each attachment and report observed versus requested state."""
~~~

Verification distinguishes `planned`, `applied`, `observed`, `propagating`, `missing`, and `mismatch`. Required mode succeeds only when every supported attachment is observed with matching policy name, target, phase, rank, and mode.

- [ ] **Step 6: Add explicit CLI plan/apply/verify wiring**

Use the smallest command surface consistent with the existing CLI:

~~~text
apx-agent agents policies SPEC.yaml plan
apx-agent agents policies SPEC.yaml apply --profile <name>
apx-agent agents policies SPEC.yaml verify --profile <name>
~~~

Plan works without credentials. Apply and verify require `--profile <name>` and a target identifier when absent from the declaration. Add CLI tests asserting profile omission fails before transport construction and secrets do not appear in output.

- [ ] **Step 7: Run native adapter tests**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/python
uv run pytest tests/test_service_policies_native.py tests/test_cli.py -q
~~~

- [ ] **Step 8: Commit native planning and verification**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_service_policies_native.py python/src/apx_agent/cli.py python/tests/test_service_policies_native.py docs/reference/service-policies-native-capabilities.md
git commit -m "feat: add native service policy plans"
~~~

### Task 6: Add observability, describe output, and user documentation

**Files:**

- Modify: `python/src/apx_agent/_audit.py` only if existing canonical attributes cannot carry required fields.
- Modify: `python/src/apx_agent/cli.py` describe output.
- Create: `docs/reference/service-policies.md`.
- Modify: `docs/reference/configuration.md`, `docs/reference/pyproject-toml.md`, `docs/safety/compliance.md`.
- Create or modify: `python/tests/test_service_policies_docs.py`, `python/tests/test_cli_describe.py`.

**Interfaces:**

- Consumes: canonical decision/plan/receipt types and existing audit helpers.
- Produces: safe operator-visible policy descriptions and documentation that separates confirmed native behavior, local approximation, and unsupported ABAC behavior.

- [ ] **Step 1: Write safe-output tests first**

~~~python
def test_describe_service_policies_omits_prompt_and_secret_values() -> None:
    output = describe_config(config_with_judge(prompt="do not print this"))
    assert "do not print this" not in output
    assert "classifier" in output
    assert "policy_name" in output


def test_dry_run_decision_contains_mode_and_adapter() -> None:
    decision = dry_run_decision()
    assert decision.mode == ServicePolicyMode.DRY_RUN
    assert decision.adapter == "local"
~~~

- [ ] **Step 2: Add canonical audit fields through existing audit machinery**

Reuse `set_audit_attrs` and existing span lookup. Add only validated names for policy name, kind, phase, rank, mode, action, adapter, and declaration fingerprint. Hash content; never record raw content. Add tests for unknown-field rejection and absent-span no-op behavior.

- [ ] **Step 3: Extend agents describe**

Show policy target, policy name/kind, phase, rank, mode, local/native mode, and ABAC status in both text and structured output. Preserve useful inspection when environment placeholders are unresolved in non-strict mode.

- [ ] **Step 4: Write the user-facing reference**

`docs/reference/service-policies.md` must include minimal TOML/YAML examples, all four built-ins and phase limits, LLM judge and SQL examples, local approximation rules, native plan/apply/verify workflow, explicit profile requirement, dry-run/enforce behavior, MCP approval/retry boundary, ABAC limitation, migration from existing guards, and secret-handling rules.

- [ ] **Step 5: Cross-link existing docs**

Add the reference to `docs/reference/configuration.md`, `docs/reference/pyproject-toml.md`, `docs/safety/compliance.md`, and `docs/README.md` without unrelated rewrites.

- [ ] **Step 6: Run documentation and CLI tests**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/python
uv run pytest tests/test_service_policies_docs.py tests/test_cli_describe.py -q
cd /Users/stuart.gano/Documents/apx-agent
git diff --check
~~~

- [ ] **Step 7: Commit documentation and observability**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/src/apx_agent/_audit.py python/src/apx_agent/cli.py docs/reference/service-policies.md docs/reference/configuration.md docs/reference/pyproject-toml.md docs/safety/compliance.md docs/README.md python/tests/test_service_policies_docs.py python/tests/test_cli_describe.py
git commit -m "docs: document service policy operations"
~~~

### Task 7: Prove end-to-end local wiring and native-plan reality

**Files:**

- Create or extend: `python/tests/test_service_policies_wiring.py`.
- Create: `python/tests/test_service_policies_reality_ctk.py`.
- Modify: `python/tests/test_compile_served_hooks.py`, `python/tests/test_executor_factory.py`, and `python/tests/test_reference_yamls.py` where existing shared-path assertions belong.

**Interfaces:**

- Consumes: canonical config, local adapter, native plan/receipt, and existing Ctk/reality-test conventions.
- Produces: proof that the declaration reaches runtime leaves, remains present in generated deployment artifacts, and is not merely accepted by a wrapper.

- [ ] **Step 1: Add the real leaf-enforcement test**

Construct an `LlmAgent` with a tool, apply `AgentConfig.service_policies`, invoke the live `_before_tool` hook, and assert a configured `DENY` prevents the tool body from running. Repeat through a composition root and an `agent_tool`-wrapped specialist.

- [ ] **Step 2: Add request/result phase tests through the actual compile path**

Compile an agent with request and result policies using existing compile fixture helpers. Assert request policies execute before the model/tool call, result policies execute after the response, and dry-run leaves the response unchanged while recording the decision.

- [ ] **Step 3: Add native plan artifact readback**

Generate a plan from a real `AgentConfig`, serialize it, read it back, and assert target, policy name, phase, rank, mode, and declaration fingerprint survive. Assert no raw prompt or secret field exists in the serialized plan.

- [ ] **Step 4: Add the Ctk reality check**

Use existing Ctk helpers to verify the generated plan artifact and live hook behavior. The test must fail if the declaration only exists in `AgentConfig` but is absent from the agent leaf or plan output.

- [ ] **Step 5: Run the focused reality suite**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/python
uv run pytest tests/test_service_policies_reality_ctk.py tests/test_service_policies_wiring.py tests/test_compile_served_hooks.py tests/test_executor_factory.py -q
~~~

- [ ] **Step 6: Commit end-to-end evidence**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add python/tests/test_service_policies_reality_ctk.py python/tests/test_service_policies_wiring.py python/tests/test_compile_served_hooks.py python/tests/test_executor_factory.py python/tests/test_reference_yamls.py
git commit -m "test: verify service policy wiring and plans"
~~~

### Task 8: Add TypeScript parity after Python contract stabilization

**Files:**

- Create: `typescript/src/service-policies.ts`.
- Create: `typescript/tests/service-policies.test.ts`.
- Modify: `typescript/src/index.ts`.
- Modify: `typescript/src/guards.ts` only where an existing hook type must accept the shared policy decision.
- Modify: `typescript/README.md` or the relevant TypeScript configuration reference.

**Interfaces:**

- Consumes: the stable Python JSON fixture shape and serialized policy contract.
- Produces: TypeScript declaration/decision parity without a second wire format.

- [ ] **Step 1: Copy the stable JSON contract into TypeScript tests**

Use the same fixture fields and assert identical values for target type, kind, phase, rank, mode, native lifecycle, and ABAC status.

- [ ] **Step 2: Implement strict TypeScript types and validation**

Define string unions for target, kind, phase, mode, and native mode. Reject missing classifier/prompt/function, invalid phase combinations, invalid rank, and unsupported `ASK` combinations before runtime.

- [ ] **Step 3: Implement ordered local decision behavior**

Match Python ordering and first-`DENY` behavior. Add dry-run tests proving the same decision record is emitted without blocking.

- [ ] **Step 4: Export the stable surface and run tests**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/typescript
npm test -- --runInBand
npm run build
~~~

- [ ] **Step 5: Commit TypeScript parity**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add typescript/src/service-policies.ts typescript/src/index.ts typescript/src/guards.ts typescript/tests/service-policies.test.ts typescript/README.md
git commit -m "feat: add TypeScript service policy parity"
~~~

### Task 9: Full verification and release evidence

**Files:**

- Modify: `CHANGELOG.md` with a concise entry after the feature is complete.
- Modify: `docs/reference/service-policies-native-capabilities.md` with the final verified surface.

**Interfaces:**

- Consumes: all previous task artifacts and tests.
- Produces: release-ready evidence separating green tests, plan-only behavior, native apply verification, and remaining beta limitations.

- [ ] **Step 1: Run narrow gates**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent/python
uv run pytest tests/test_service_policies.py tests/test_service_policies_local.py tests/test_service_policies_native.py tests/test_service_policies_wiring.py tests/test_service_policies_reality_ctk.py -q
cd /Users/stuart.gano/Documents/apx-agent
git diff --check
pre-commit run --all-files
~~~

- [ ] **Step 2: Run the repository gate**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
make check
~~~

Expected: the full suite, including reality checks, passes; the lockfile verifier does not rewrite unrelated content.

- [ ] **Step 3: Verify native behavior only with an explicitly selected profile**

After the user selects a profile, run plan first, then apply and verify only against the declared non-production test target:

~~~bash
apx-agent agents policies <SPEC.yaml> plan
apx-agent agents policies <SPEC.yaml> apply --profile <PROFILE>
apx-agent agents policies <SPEC.yaml> verify --profile <PROFILE>
~~~

Record target, policy identity, native response status, propagation state, and observed enforcement. Do not claim native apply from a successful command wrapper alone.

- [ ] **Step 4: Update changelog and capability record**

Document exactly which operations are native, plan-only, local-mirror-only, or unsupported. Include the current ABAC limitation and explicit profile requirement.

- [ ] **Step 5: Inspect final diff and status**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git status --short --branch
git diff origin/main...HEAD --stat
git diff origin/main...HEAD --check
~~~

Confirm unrelated dirty files were not staged or committed.

- [ ] **Step 6: Commit release evidence**

~~~bash
cd /Users/stuart.gano/Documents/apx-agent
git add CHANGELOG.md docs/reference/service-policies-native-capabilities.md
git commit -m "docs: record service policy release evidence"
~~~

## Plan self-review

- PRD coverage: Tasks 2–4 cover canonical declaration, local mirror, phases, rank, modes, built-ins, LLM judge, SQL fail-closed behavior, approval restrictions, and backward compatibility. Task 5 covers native plan/apply/verify, explicit profiles, capability failures, and ABAC status. Task 6 covers audit, describe output, documentation, and migration. Task 7 covers claim-versus-reality proof. Task 8 covers delayed TypeScript parity. Task 9 covers full verification and release evidence.
- Placeholder scan: no task depends on an unspecified class, function, command, or test name; the native transport is explicitly selected by Task 1 and has a concrete plan-only failure path when the current platform surface cannot apply attachments.
- Type consistency: `ServicePoliciesConfig`, `ServicePolicyAttachment`, `ServicePolicy`, `ServicePolicyEvent`, `ServicePolicyDecision`, `NativePolicyPlan`, `NativePolicyApplyReceipt`, and `NativePolicyVerification` are the shared names used across tasks. Local and native adapters consume the same canonical config.
- Scope guard: Python local/native behavior is the first independently testable slice; documentation/reality proof follows; TypeScript parity is intentionally last and does not block the Python release.
