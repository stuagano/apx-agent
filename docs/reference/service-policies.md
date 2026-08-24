# Service Policies

Service Policies provide one declaration for governance that can be mirrored
locally and projected into Databricks native policy attachments. The declaration
lives under AgentConfig, so YAML, pyproject.toml, generated Apps projects, and
runtime wiring use the same validated contract.

The native concepts follow the [Databricks Service Policy Beta contract](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/create-service-policy):
ON CALL evaluates requests, ON RESULT evaluates responses, rank controls
ordering, and evaluation stops at the first DENY.

## Minimal configuration

~~~toml
[tool.apx.agent.service_policies]
local_mode = "mirror"       # mirror | off
native_mode = "plan"        # off | plan | apply | required

[[tool.apx.agent.service_policies.attachments]]
name = "github-guardrails"
target_type = "mcp_service"
target = "main.tools.github"
mode = "enforce"            # enforce | dry_run

[[tool.apx.agent.service_policies.attachments.policies]]
name = "jailbreak-defense"
kind = "builtin"
builtin = "jailbreak"
phase = "on_call"
rank = 100

[[tool.apx.agent.service_policies.attachments.policies]]
name = "external-write-review"
kind = "llm_judge"
classifier = "databricks-claude-haiku-4-5"
prompt = "Require approval for destructive external writes."
phase = "on_call"
rank = 200

[[tool.apx.agent.service_policies.attachments.policies]]
name = "sql-policy"
kind = "sql"
function = "main.governance.check_event"
phase = "on_result"
rank = 300

[tool.apx.agent.service_policies.abac]
tags = { service_class = "customer-facing" }
~~~

The same shape is accepted in YAML:

~~~yaml
name: customer-agent
service_policies:
  native_mode: plan
  attachments:
    - name: mcp-guardrails
      target_type: mcp_service
      target: main.tools.github
      mode: dry_run
      policies:
        - name: jailbreak-defense
          kind: builtin
          builtin: jailbreak
          phase: on_call
          rank: 100
~~~

Unknown fields, missing policy references, invalid ranks, empty targets, and
invalid phase combinations fail during configuration validation. YAML resolves
$ENV_VAR references before validation and rejects unresolved placeholders in
strict run/deploy mode.

## Targets and phases

The portable target types are:

- mcp_service
- model_service
- model_provider_service
- agent_service (portable intent; native support is capability-checked)

Policies can use on_call, on_result, or both. jailbreak is request-only;
hallucination is result-only. For request evaluation, lower ranks run first.
For result evaluation, higher ranks run first. The first DENY or ASK ends that
phase. A dry-run decision retains its real action in the audit record but does
not block the request.

## Policy kinds

### Built-in policies

The stable identifiers are:

- sensitive_data
- unsafe_content
- jailbreak
- hallucination

Databricks owns the native managed classifiers. The local mirror uses the
existing prompt-injection heuristic for jailbreak. The other managed
classifiers require an injected evaluator or Watchdog transport; enforce mode
fails closed when neither is available. Local equivalence is not claimed for a
Databricks-managed classifier unless the same evaluator is used. Sensitive data
redaction remains a native capability; apx-agent does not invent local
redaction semantics.

### Custom LLM-as-a-judge

llm_judge requires both classifier and prompt. Locally it reuses the existing
PromptPolicy, including its fail-closed behavior for classifier transport
failures and malformed verdicts. The full rubric is never emitted in plans or
audit attributes.

### Custom SQL policy

sql requires a Unity Catalog function reference. The native contract is a SQL
function taking one event VARIANT and returning a VARIANT with result set to
ALLOW, DENY, or ASK, plus an optional reason. apx-agent does not interpret SQL
in Python. Without an injected local evaluator, enforce mode denies and
dry-run records UNAVAILABLE.

## Enforce, dry-run, and approval behavior

enforce is fail-closed: evaluator failures, malformed results, unavailable
capabilities, and missing local implementations cannot become ALLOW. dry_run
records the decision and allows the local request to continue. Keep the mode
visible in operator output and audit data; dry-run is not enforcement.

Native ASK is supported only for a live MCP request (mcp_service + on_call).
Locally, tool-call ASK reuses PolicyGate and ApprovalStore: the first attempt
raises ApprovalRequired, approval is one-shot and bound to the exact tool
arguments, and the identical retry consumes the approval. The local mirror
does not create a new approval surface for model output or result hooks.

## Native plan, apply, and verify

Generate a sanitized plan without credentials:

~~~bash
apx-agent agents policies agent.yaml plan
~~~

The plan includes target, policy identity, kind, phase, rank, mode, and a
declaration fingerprint. It omits classifier prompts, SQL bodies, request
payloads, and credentials.

Apply and verify require an explicit profile:

~~~bash
apx-agent agents policies agent.yaml apply --profile <name>
apx-agent agents policies agent.yaml verify --profile <name>
~~~

apx-agent never selects the ambient Databricks profile implicitly. The current
installed SDK has no verified Service Policy attachment or read-back client, so
native apply and verification remain plan-only and fail with a capability error.
The implementation must not guess an undocumented endpoint. See the [native
capability record](service-policies-native-capabilities.md) for the verified
boundary.

native_mode = "required" means the operation is unsuccessful unless every
declared policy can be represented and verified natively. It is appropriate for
deployment gates, not for local development until the target platform surface is
available.

## ABAC status

ABAC tags are accepted and serialized as desired state:

~~~toml
[tool.apx.agent.service_policies.abac]
tags = { service_class = "customer-facing" }
~~~

Current Service Policy Beta attachments remain service-specific. Tag-based
attachment across existing and future matching services is the intended scale
mechanism, but it is reported unsupported until the native Service Policy
attachment API supports it. This is distinct from the broader [Unity Catalog
ABAC model](https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac/grant-policies).

## Audit and secret handling

Decisions emit only validated policy name, kind, phase, rank, mode, action,
adapter, and a declaration fingerprint through the existing audit attribute
machinery. Prompts, SQL bodies, raw messages, tool arguments, model responses,
tokens, and credentials are not logged or included in native plans.

## Migration from existing guards

Existing [tool.apx.agent.guardrails], code-defined hooks, PolicyGate, and
WatchdogGuard remain supported. Service Policy hooks are additive and are
attached through the same finalize_agent seam used by Apps and model-serving
paths. Code-defined hooks run first; the declarative local mirror runs after
them. Migrate one policy at a time, run with dry_run, inspect audit decisions,
then change the attachment to enforce.
