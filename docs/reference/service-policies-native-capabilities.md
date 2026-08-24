# Native Service Policy Capabilities

This record freezes the native capability boundary used by apx-agent's first
Service Policy implementation. It is intentionally separate from workspace
credentials and from any one Databricks profile.

## Verified now

The official [Service Policy documentation](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/create-service-policy)
documents these Beta concepts:

- policies attach to MCP services, model services, and model provider services;
- request policies run at `ON CALL` and result policies run at `ON RESULT`;
- lower rank runs first for requests and higher rank runs first for results;
- built-in guardrails, LLM-as-a-judge policies, and custom SQL policies are
  represented as service-policy attachments;
- native `ASK` is limited to the live MCP request phase;
- current attachment is service-specific, while ABAC attachment remains a
  scaling limitation documented in the product rationale.

The installed `databricks-sdk` was inspected during implementation. Its
`WorkspaceClient` exposes cluster, tag, and other policy clients, but no
Service Policy attachment, apply, or read-back client. No documented REST
endpoint was present in the repository's existing helpers.

## Plan-only operations

The following operations are supported without workspace access:

- validate a `ServicePoliciesConfig` declaration;
- validate target, policy kind, phase, mode, rank, and `ASK` combinations;
- emit a deterministic, sanitized native operation plan;
- report unsupported native capabilities and ABAC selectors;
- hash declaration identity without serializing prompts, SQL bodies, or
  credentials.

## Apply operations

Native apply is currently plan-only in apx-agent. The implementation must not
invent an endpoint or call an undocumented API. `apply` therefore raises a
capability error until a supported Databricks SDK or documented REST surface is
verified and added behind the native transport boundary.

When native apply becomes available, it must require an explicit
`--profile <name>` and a non-production target chosen by the operator. The
ambient `DATABRICKS_CONFIG_PROFILE` must not be used as an implicit target.

## Verification operations

Native read-after-write verification is also plan-only until an attachment
read-back surface is verified. The future verifier must compare the observed
target, policy identity, phase, rank, and mode, and must distinguish missing,
mismatched, and still-propagating state.

## Unsupported beta behavior

- ABAC selectors are accepted as desired state in the portable declaration but
  are reported unsupported for native apply until the service-policy attachment
  surface supports tag-based selection.
- Local execution cannot reproduce Databricks-managed sensitive-data,
  unsafe-content, and hallucination classifiers without an injected evaluator
  or configured Watchdog transport.
- Native service-policy SQL execution remains Databricks-owned; apx-agent does
  not interpret SQL UDFs locally.

## Required permissions and profile inputs

Plan generation requires no Databricks credentials. Apply and verify must use
an explicitly selected CLI/SDK profile and must fail before transport
construction when the profile is missing. Any future permission requirements
must be recorded here from the verified API contract rather than inferred from
unrelated Unity Catalog or cluster-policy permissions.

The capability fixture is
`python/tests/fixtures/service_policy_capabilities.json`.
