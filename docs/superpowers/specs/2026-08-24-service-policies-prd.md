# Service Policies for apx-agent — Product Requirements Document

**Status:** Draft for review
**Date:** 2026-08-24
**Owner:** apx-agent
**Scope:** Python declaration/runtime first; native Databricks deployment adapter; TypeScript parity after the Python contract is stable

## 1. Executive summary

apx-agent should absorb Databricks Unity AI Gateway Service Policies as a
portable, declarative governance capability. A user should be able to declare
the policies attached to the AI services their agent uses once, then receive:

1. a local mirror for development, offline tests, and non-native targets; and
2. native Databricks enforcement when the deployment target and currently
   available platform surface support it.

The declaration must remain the source of truth. Local execution and native
deployment are projections of that declaration, not separate policy systems.

This work reuses apx-agent's existing `PolicyGate`, `ApprovalStore`,
`WatchdogGuard`, lifecycle hooks, audit attributes, Pydantic configuration
models, YAML loader, project generator, and shared `finalize_agent` wiring
seam. It does not create a new general-purpose compliance engine.

## 2. Product context and research

Databricks describes a Service Policy as a guardrail scoped to an AI service.
Unity AI Gateway routes model and MCP traffic, while Unity Catalog governs the
underlying AI securables and their access. Service Policies govern behavior at
the interaction level: allow, deny, or request approval based on the request,
response, caller, and service context.

Authoritative platform references reviewed for this PRD:

- [AI governance with Unity AI Gateway](https://docs.databricks.com/aws/en/ai-gateway)
- [AI governance guide](https://docs.databricks.com/aws/en/ai-gateway/ai-governance)
- [Create and attach a service policy](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/create-service-policy)
- [Sensitive Data Detection](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/detect-sensitive-data)
- [ABAC GRANT policies](https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac/grant-policies)

### 2.1 Confirmed beta contract

The current beta contract relevant to apx-agent is:

- A custom service-policy function is a Unity Catalog SQL UDF with one
  `event VARIANT` parameter and a `VARIANT` return value.
- The returned decision has a top-level `result` of `ALLOW`, `DENY`, or
  `ASK`, plus an optional `reason`.
- The input phase is `ON CALL` and is represented in the SQL event as
  `event:type = 'request'`.
- The output phase is `ON RESULT` and is represented as
  `event:type = 'response'`.
- Multiple attachments use a rank. The lowest rank runs first on input and
  last on output. Evaluation stops at the first `DENY`.
- `ASK` is supported for live MCP request approval. Native external MCP
  approval uses MCP URL-mode elicitation and requires a compatible MCP client
  retry path.
- Built-in policies include sensitive-data detection, unsafe-content
  detection, jailbreak detection, and hallucination detection. Exact
  service/phase support is platform-defined and must be capability-checked at
  native apply time.
- Sensitive-data detection can block or redact in the native beta. Native
  policy transformations are not generally available to custom SQL policies;
  the native adapter must not claim arbitrary custom redaction support.
- Custom LLM-as-a-judge policies identify an evaluator/classifier model and
  carry a classifier prompt/rubric.
- The current beta is attached to individual services and has UI-oriented
  limitations. Catalog/schema-level ABAC attachment and custom principal
  scoping are not yet available for Service Policies, even though ABAC is the
  intended scale-out direction.
- Service-policy changes can take time to propagate. Native verification must
  distinguish “attachment accepted” from “policy observed enforcing.”

The PRD treats the current platform limitations as explicit capability facts,
not as reasons to hide native support behind an apx-agent-only implementation.

### 2.2 Repository capabilities to absorb

The repository already contains primitives that cover meaningful portions of
the product:

| Existing primitive | Reuse in Service Policies |
| --- | --- |
| `PolicyGate` in `python/src/apx_agent/_policy.py` | Local `ALLOW`/`ASK`/`DENY` decision bridge and approval retry semantics |
| `ApprovalStore` | Local single-process approval state; preserve the existing durable-backend ceiling |
| `PromptPolicy` | Local LLM-as-a-judge mirror with fail-closed parse/transport behavior |
| `WatchdogClient` / `WatchdogGuard` | External governance transport and redaction/reporting integration |
| `compose` and `build_config_guards` in `_guards.py` | Existing hook composition and fast local checks |
| `input_guardrails`, `output_guardrails`, `before_tool`, `before_model` | Local phase projections |
| `set_audit_attrs` and `apx.watchdog.*` attributes | Decision and dry-run audit evidence |
| `AgentConfig` / `GuardrailsConfig` in `_models.py` | Declarative Pydantic contract and strict validation |
| `apply_config_knobs` / `apply_config_guardrails` in `_wiring.py` | Shared config-to-live-agent seam used by run and deploy paths |
| `_yaml_spec.py` | YAML spec loading, environment resolution, and strict validation |
| `_project_gen.py` | Generated `pyproject.toml` and Apps project projection |
| `agents describe` and existing config docs | Discoverable policy declaration and operator diagnostics |

The new feature must not fork these paths. In particular, a policy that works
under `apx-agent run` but disappears during model-serving or Apps deployment is
a release-blocking defect.

## 3. Problem statement

Today apx-agent has local guard configuration and an external Watchdog adapter,
but it does not expose a stable declaration for the newer Databricks Service
Policy model. Users cannot describe, validate, inspect, mirror, or deploy the
same policy intent across:

- model services;
- model provider services;
- MCP services; and
- agent services.

Without a canonical contract, users must manually configure native policies in
the platform and separately wire local hooks. That creates drift in policy
names, phases, ordering, approval behavior, dry-run semantics, and audit
evidence. It also makes a future ABAC selector impossible to express without
another migration.

## 4. Goals

### G1. One portable declaration

Add a strict, serializable Service Policy declaration under `AgentConfig` that
can describe policy attachments independently of the current deployment
substrate.

### G2. Local mirror

Project the declaration into existing local hooks and policy primitives for
development, tests, and targets without native Service Policy enforcement.

The mirror must make parity claims explicit. Exact parity is required for
phase selection, rank ordering, short-circuiting, `ASK` restrictions, mode
semantics, and fail-closed behavior. Semantic parity for vendor-managed
detectors is approximate unless the same native evaluator is used.

### G3. Native Databricks projection

Project the same declaration into a deterministic native deployment plan and,
where a supported Databricks API/SDK surface exists, apply and verify native
attachments to the declared service.

Native apply must fail closed when a requested policy cannot be represented or
the required platform capability is unavailable. It must never silently
downgrade a requested native policy to a local-only hook.

### G4. Beta guardrail coverage

Represent the four beta built-ins:

- sensitive data;
- unsafe content;
- jailbreak, input/request phase;
- hallucination, output/result phase.

Represent custom LLM-as-a-judge policies with an evaluator/classifier name and
prompt/rubric. Represent custom SQL policies by Unity Catalog function name,
with the `event VARIANT` → `VARIANT` contract and fail-closed behavior.

### G5. Operational safety

Support per-attachment `enforce` and `dry_run` modes, rank ordering, audit
metadata, explicit approval constraints, and human-readable diagnostics.

### G6. ABAC-ready intent

Represent future tag-based selectors and policy scope as desired state without
pretending the current beta can apply them. When native ABAC support becomes
available, the declaration should be extensible without changing the local
policy contract.

### G7. Documentation and discoverability

Document the declaration, capability matrix, local/native differences, failure
semantics, and migration path from existing `guardrails`/Watchdog wiring.

## 5. Non-goals

- Building a new cross-domain compliance or violation-lifecycle engine.
- Replacing Databricks Unity AI Gateway, Unity Catalog, or Watchdog.
- Implementing a general SQL interpreter in Python.
- Claiming exact local equivalence for Databricks-managed classifiers without
  the same evaluator and model.
- Automatically creating or modifying production policies without an explicit
  deployment/apply action.
- Automatically selecting a Databricks workspace profile.
- Implementing catalog/schema-level ABAC application before the native API
  supports it.
- Adding TypeScript parity before the Python declaration and native/local
  semantics are stable.
- Reworking unrelated guard, approval, tracing, or deployment architecture.

## 6. Users and primary workflows

### 6.1 Agent author

Declares a policy attachment in YAML or `pyproject.toml`, runs local tests,
and sees what will be enforced locally versus natively.

### 6.2 Platform operator

Reviews a native deployment plan, applies it with an explicit target/profile,
and verifies that the policy is attached and observable.

### 6.3 Governance owner

Defines SQL functions, evaluator models, ranks, modes, and future tag
selectors; reviews audit evidence and propagation status.

### 6.4 Developer in dry-run mode

Receives an allowed response while the policy decision is recorded with the
policy name, phase, rank, mode, reason, and target. Dry-run must never be
mistaken for enforcement in the CLI, logs, or docs.

## 7. Proposed product contract

### 7.1 Configuration shape

The public model should use focused Pydantic types rather than an untyped
dictionary. The exact class names may follow repository naming conventions,
but the contract must express these concepts:

```toml
[tool.apx.agent.service_policies]
local_mode = "mirror"       # mirror | off
native_mode = "plan"        # off | plan | apply | required

[[tool.apx.agent.service_policies.attachments]]
name = "github-guardrails"
target_type = "mcp_service"
target = "main.tools.github"
mode = "enforce"            # enforce | dry_run

[[tool.apx.agent.service_policies.attachments.policies]]
name = "block-sensitive-data"
kind = "builtin"
builtin = "sensitive_data"
phase = "both"              # on_call | on_result | both
rank = 100

[[tool.apx.agent.service_policies.attachments.policies]]
name = "no-repository-delete"
kind = "sql"
function = "main.governance.no_repository_delete"
phase = "on_call"
rank = 200

[[tool.apx.agent.service_policies.attachments.policies]]
name = "review-external-write"
kind = "llm_judge"
classifier = "databricks-claude-haiku-4-5"
prompt = "Require approval for external writes."
phase = "on_call"
rank = 300

[tool.apx.agent.service_policies.abac]
tags = { service_class = "customer-facing" }
```

The equivalent YAML shape must be supported for the existing spec-driven
workflow. `native_mode = "required"` means a deployment is unsuccessful unless
every declared attachment is represented and verified natively. `plan` emits
the native plan and does not mutate the external workspace.

The declaration must distinguish:

- policy definition identity (`name`, `kind`, built-in/function/classifier
  reference);
- attachment target (`target_type`, `target`, principal scope, future
  selector);
- evaluation behavior (`phase`, `rank`, `mode`); and
- native lifecycle (`off`, `plan`, `apply`, `required`).

### 7.2 Supported policy kinds

#### Built-in

Required built-in identifiers:

```text
sensitive_data
unsafe_content
jailbreak
hallucination
```

The public model should use stable apx-agent identifiers and map them to
Databricks names in a capability table. It must not bake undocumented native
function names into the public API.

#### LLM-as-a-judge

Required fields:

- evaluator/classifier model or service name;
- prompt/rubric;
- phase;
- rank;
- mode.

The local adapter may reuse `PromptPolicy`. The native adapter must preserve
the evaluator and prompt in its plan and reject unsupported fields rather than
silently dropping them.

#### SQL policy

Required fields:

- Unity Catalog SQL function name;
- phase;
- rank;
- mode.

The native adapter treats the function as an existing governed dependency and
verifies the required function reference/privileges where the platform API
allows. The local adapter does not interpret SQL. In local `enforce` mode, a
SQL policy without an executable local evaluator fails closed; in `dry_run`, it
records an unavailable evaluator as a non-enforcing diagnostic.

### 7.3 Targets

The declaration must support these target types:

- `model_service`;
- `model_provider_service`;
- `mcp_service`; and
- `agent_service` when the native surface supports it.

Target capability is not inferred from a string. A native capability resolver
must return a structured result containing supported kinds, phases, actions,
and attachment operations. Unsupported combinations produce actionable
errors naming the target, policy, phase, and missing capability.

### 7.4 Phases and ordering

Public phase values are:

- `on_call`;
- `on_result`; and
- `both`.

Local event mapping:

| Public phase | Local event | Native event |
| --- | --- | --- |
| `on_call` | request/input/tool/model hook | `request` / ON CALL |
| `on_result` | response/output/tool-result hook | `response` / ON RESULT |
| `both` | both local phases | both native phases |

For each phase, policies are evaluated by rank. A `DENY` stops the phase
immediately. An `ASK` returns an approval-required result and pauses the
current attempt; the retry must carry the same action fingerprint. An `ALLOW`
continues to the next policy.

The implementation must keep input and output ordering distinct: ascending
rank for input and reverse rank for output. This is a core compatibility rule,
not a presentation detail.

### 7.5 Modes

`enforce`:

- `DENY` blocks the interaction;
- `ASK` requests approval where supported;
- `ALLOW` proceeds;
- failures in a configured policy evaluator fail closed.

`dry_run`:

- the policy evaluates and emits an audit/diagnostic decision;
- the interaction proceeds regardless of `DENY` or `ASK`;
- evaluator failures remain visible and are marked as non-enforcing;
- the result must include `mode = dry_run` so consumers cannot confuse it with
  an enforced decision.

Native responses may represent `DENY` as a successful HTTP response with a
`databricks_service_policy` payload rather than a transport error. The local
adapter should normalize both native and local outcomes into a shared internal
decision record while preserving the original surface behavior at the edge.

### 7.6 ASK constraints

The initial native contract permits live approval only for MCP request-phase
policy evaluation. Therefore:

- native `ASK` on `mcp_service` + `on_call` is supported;
- native `ASK` on model/provider/agent services is rejected during native
  capability validation unless the platform capability resolver explicitly
  reports support;
- native `ASK` on `on_result` is rejected;
- local `ASK` may reuse `PolicyGate` for supported in-process tool calls, but
  its broader local capability must not be presented as native parity.

## 8. Architecture and data flow

```text
YAML / pyproject.toml / Python config
                |
                v
       ServicePolicyConfig
                |
        canonical validation
                |
       +--------+---------+
       |                  |
       v                  v
 LocalPolicyAdapter   NativePolicyAdapter
       |                  |
 PolicyGate, hooks,   plan / apply / verify
 PromptPolicy,        Databricks AI Gateway /
 WatchdogGuard        Unity Catalog surface
       |                  |
       +--------+---------+
                v
       Decision + audit record
```

The canonical layer owns validation, phase/rank ordering, mode, capability
errors, and decision metadata. Adapters own transport-specific behavior.

The local adapter must attach through the existing shared config seam so both
`setup_agent` and model-serving deploy capture the same policy behavior. It
must walk the same agent leaves as existing declarative guardrails and must
preserve code-defined hook ordering unless the policy declaration explicitly
requests a supported precedence.

The native adapter must be side-effect-free in `plan` mode. In `apply` mode it
must use an explicitly selected Databricks profile supplied by the caller; it
must never rely on the ambient `DATABRICKS_CONFIG_PROFILE` silently. Apply
must return a receipt containing target, attachment identity, requested
policy, native status, and verification status.

## 9. Error handling and fail-closed rules

The following are hard errors:

- unknown policy kind, built-in identifier, target type, phase, mode, or
  native mode;
- missing required classifier, prompt, function, or target fields;
- invalid phase/kind combinations, including jailbreak on result or
  hallucination on call;
- native `ASK` outside the supported MCP request phase;
- rank that is not a finite non-negative integer;
- local enforce mode with no executable evaluator for a custom SQL policy;
- native apply when required capability, permission, or target resolution is
  missing;
- malformed SQL/native decision responses;
- failed verification when `native_mode = "required"`.

No policy evaluator exception may be converted to `ALLOW` in enforce mode.
Dry-run may continue the user interaction, but it must preserve the failure in
the decision and audit record.

## 10. Observability and audit

Every evaluated attachment should make available:

- policy name and kind;
- target type and target identifier;
- phase;
- rank;
- mode;
- action (`ALLOW`, `DENY`, `ASK`, or `UNAVAILABLE` for dry-run diagnostics);
- reason;
- evaluator/classifier/function reference without secrets;
- native/local adapter;
- policy version or declaration fingerprint;
- principal/session/request identifiers where available;
- evaluation latency;
- approval ID and approver identity for local `ASK`.

Use existing audit/tracing primitives and add only canonical policy-specific
attributes. Raw prompts, raw sensitive values, SQL secrets, and credentials
must never be emitted into logs or user-facing diagnostics.

## 11. Backward compatibility and migration

Existing configuration remains valid:

- `[tool.apx.agent.guardrails]` continues to work unchanged;
- explicit code-defined hooks continue to run;
- Watchdog integrations continue to work;
- agents without `service_policies` have no new runtime behavior;
- native policy application is opt-in through `native_mode`.

Migration guidance should show:

1. existing `injection_detection` → built-in/local jailbreak mirror;
2. existing `PolicyGate` → local Service Policy `ASK`/`DENY` projection;
3. existing `PromptPolicy` → local LLM-as-a-judge policy;
4. existing `WatchdogGuard` → external/custom policy adapter; and
5. existing manually attached Databricks policies → declarative references
   with `native_mode = "plan"` before any apply.

No automatic migration should rewrite user files or attach native policies.

## 12. Rollout and verification

### Phase 0 — contract and capability inventory

- Freeze the Pydantic/YAML/TOML contract.
- Inventory current Databricks SDK/REST support without selecting a workspace
  profile or mutating external state.
- Record native capability gaps and exact plan/apply behavior.

### Phase 1 — local mirror

- Add canonical models and validation.
- Add shared phase/rank/mode evaluator.
- Project built-ins, LLM judges, SQL references, and approval constraints into
  local adapters.
- Add unit, integration, and reality tests for decision behavior and wiring.

### Phase 2 — native plan and verification

- Emit deterministic native policy plans.
- Add explicit apply only for supported operations.
- Add receipt and read-after-write verification.
- Add `native_mode = required` enforcement.

### Phase 3 — ABAC-ready selectors

- Persist selectors in the declaration and plan output.
- Show unsupported/current-beta status clearly.
- Add native selector application only when a verified API supports it.

### Phase 4 — TypeScript parity

- Port the stable canonical contract and local decision semantics.
- Reuse the same serialized policy shape and compatibility fixtures.

## 13. Acceptance criteria

### Contract

- A strict Pydantic model accepts valid Python/YAML/TOML declarations and
  rejects unknown keys and invalid combinations.
- `agents describe` shows policy attachments, phases, ranks, modes, native
  mode, and ABAC selector status without exposing secrets.
- Generated Apps projects preserve the declaration.

### Local behavior

- Policies run on request and response phases with the correct rank direction.
- The first `DENY` short-circuits later policies in that phase.
- `ASK` creates or consumes an approval through the existing approval model.
- `ASK` is rejected for unsupported native combinations.
- `dry_run` never blocks but records the decision and evaluator failures.
- SQL policies fail closed in enforce mode when no executable local evaluator
  exists.
- Existing code hooks, `GuardrailsConfig`, Watchdog, and composition agents
  retain their behavior.

### Native behavior

- Plan mode produces deterministic, reviewable output without external writes.
- Apply mode requires an explicit profile and target and never auto-selects a
  profile.
- Unsupported native capabilities fail with target/policy/phase-specific
  diagnostics.
- Supported attachment operations are read back and verified.
- `native_mode = required` fails deployment if any declared policy is not
  natively represented and verified.

### Safety and evidence

- No credentials, prompts containing sensitive content, raw request payloads,
  or secrets appear in logs or receipts.
- The test suite includes at least one reality test that proves the local
  declaration reaches the live agent leaf and one native-plan read-after-write
  or fixture verification test.
- Documentation distinguishes confirmed native behavior, local approximation,
  unsupported beta features, and future ABAC capability.

## 14. Open decisions for implementation review

These are intentionally narrow implementation decisions, not reasons to defer
the product:

1. Whether the first native adapter uses an installed Databricks SDK surface,
   REST calls, or plan-only output when no supported attachment endpoint is
   exposed. The implementation plan must verify this before adding a client.
2. Whether service-policy config is nested under the existing
   `[tool.apx.agent]` block or introduced as a sibling `[[tool.apx.service_policies]]`
   table. The first option is preferred for discoverability and consistency
   with `guardrails`.
3. Which existing audit attribute names can be reused without expanding the
   canonical schema unnecessarily.
4. Which local approximation is selected for each vendor-managed built-in when
   no native evaluator is available. The chosen behavior must be explicit in
   capability output and tests.

## 15. Success definition

An apx-agent author can declare a Service Policy once, inspect exactly what it
means locally and natively, test it without a workspace, produce a reviewable
native plan, and—when the platform supports the requested operation—apply and
verify native enforcement without manual policy drift. Future ABAC selectors
can be added to the same declaration without replacing the runtime contract.
