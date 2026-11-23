# Attach an external guardrail on the services an agent uses

An external service policy sends each governed call to a guardrail vendor you
already use. Databricks enforces the vendor's verdict. apx leaves that
enforcement on the gateway: it does not call the vendor, and it does not
attach the policy.

The policy belongs on the Unity AI Gateway service the agent calls. That is
the model service named by `model=`, or an MCP service the agent binds.
`guardrails=` on the agent configures in-process hooks. `service_policies=`
is the portable policy declaration in
[service-policies.md](../reference/service-policies.md). The Unity Gateway
attachment is the UI procedure below.

## Why deploy does not check the attachment

Rechecked 2026-11-23. Attachment is a UI step.
[Enforce a partner guardrail with an external service policy](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/external-policies)
(page updated 2026-11-18) says: "You attach external service policies through
the Unity Gateway UI. Attaching them through the REST API or Terraform isn't
available."

The same check, the same day:

- [The ModelService object](https://docs.databricks.com/api/ai-gateway/v1/model-service/object.md)
  has no policy field. `GET /api/2.1/unity-catalog/{name=model-services/*}`
  returns routing, rate limits, and the inference table.
- `databricks-sdk` 0.170.0 (released 2026-11-23) `ModelService` and
  `ModelServiceConfig` match that object. `WorkspaceClient.ai_gateway`
  creates, reads, updates, and deletes model services, model provider
  services, MCP services, skills, and MCP user credentials. It has no
  service-policy method. `databricks/sdk/service/catalog.py` in that release
  does not contain `service_policies`.
- [databricks_ai_gateway_model_service](https://registry.terraform.io/providers/databricks/databricks/latest/docs/resources/ai_gateway_model_service)
  (registry page published 2026-11-23) manages the model service's routing
  config. It has no service-policy argument.
- Serving-endpoint guardrails (`AiGatewayGuardrails` in the SDK serving
  package) are a different product. They do not govern the Unity Catalog AI
  Gateway chat call.
- [Create and attach a service policy](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/create-service-policy)
  (fetched 2026-11-23) says policy attachment is UI-only. The
  [service policies overview](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/)
  (updated 2026-11-18) tells API callers to set `options.phases` on the
  Jailbreak guardrail. That sentence does not publish a list or get operation
  for attachments, and the AI Gateway API index has no service-policy
  resource. There is no response shape a deploy check can read.

`apx-agent doctor` therefore WARNs. It names the model service and any
declared `service_policies` attachment targets, and it calls no API. Deploy
does not fail closed on a declared external policy, because a check that
cannot read the attachment cannot tell a missing policy from an attached one.

## Attach the policy

You need `MANAGE` on the service, `USE CONNECTION` on the vendor connection,
and `USE CATALOG` and `USE SCHEMA` on that connection's parents. The
connection is a Unity Catalog HTTP connection with OAuth machine-to-machine
credentials. Create it ahead of time, or create it from the policy form.

1. In the workspace sidebar, click **AI Gateway**.
2. Select the service: a model service on the Models tab, a model provider
   service on the Providers tab, or an MCP service on the MCPs tab.
3. Open the **Policies** tab, then click **New policy**.
4. Enter a name.
5. In **Guardrail type**, select **External**.
6. Set the rank. The lowest rank runs first on the request.
7. Under **Phase**, select input, output, or both.
8. Select the connection, or create one.
9. Optionally enter a JSON policy configuration. Databricks passes that text
   to the vendor unchanged.
10. Under **Advanced options**, select a mode. **Log** records the would-be
    verdict and lets the call through. **Enforce** applies the verdict.
    Start in Log, review traffic, then switch to Enforce.
11. On a model or model provider service, optionally select full response
    evaluation.
12. Click **Create policy**.

Wait 60 to 90 seconds before testing. A change inside that window looks like
the policy has no effect.

One policy attaches to one service. Repeat the steps for each model service
and each MCP service the agent uses.

## What a DENY looks like

In Enforce mode the gateway returns HTTP 200 for a chat-style model call.
The assistant text names the policy, and the body carries a top-level
`databricks_service_policy` object. apx raises `GatewayRefusal` (a
`PermissionError`) and writes the service-policy audit attributes. The
prompt is left out of the message and out of those attributes.

A model call:

```text
The Unity AI Gateway refused this model call (policy <name>): <reason>
```

An MCP denial is a JSON-RPC error. The reason is `error.message`, and
`error.data` may carry the same `databricks_service_policy` object. apx
turns that into the same typed refusal on the tool error:

```text
The Unity AI Gateway refused this MCP call (policy <name>): <reason>
```

When the vendor omits a reason, the gateway uses:

```text
Access denied: this request is not permitted by a policy on this service.
```

In Enforce mode the vendor is on the critical path. The endpoint must answer
within 5 seconds with HTTP 200 and a verdict of `ALLOW`, `DENY`, or `ASK`.
A timeout, a connection failure, an HTTP error, or any other verdict is a
denial. Log mode records that failure and lets the call through. The vendor
contract is the
[external service policy API reference](https://docs.databricks.com/aws/en/data-governance/unity-catalog/service-policies/external-policy-api)
(page updated 2026-11-19).

## What an ASK looks like

On an MCP tool call, `ASK` holds the call until a person approves it.
Databricks delivers that to an external agent as MCP URL-mode elicitation
(protocol 2025-11-25): JSON-RPC `-32042` with `error.data.elicitations`
entries of `mode="url"`. apx surfaces the hold and any plain http(s)
approval URL:

```text
The Unity AI Gateway is holding this MCP call for approval (policy <name>): <reason>. Tell the user this action needs their approval and to open <url>. Retry the same call only after they approve it.
```

The gateway owns the approval. apx records the service-policy audit action
`ASK`, leaves the approval URL out of the audit attributes, and leaves the
retry to the caller after the person approves. It does not write an apx
`Approval` row for this hold.

An `ASK` on a model service is a block. A model service cannot pause:

```text
The Unity AI Gateway refused this model call (policy <name>): <reason>. A model service cannot pause for approval.
```

An `ASK` on an MCP tool result lets the result through. Hiding that result
would drop a call the gateway already allowed, so apx leaves it on the
normal tool path. To block a result, the vendor returns `DENY`.

## What doctor prints

For an Apps project with a declared model, `apx-agent doctor` adds a WARN
named `AI Gateway guardrails (<model>)`. The detail says attachment is
UI-only and lists each unverified service, including a `databricks-<id>`
model rewritten to `model-services/system.ai.<id>`. The fix names the UI
steps above and says an ASK holds an MCP call and blocks a model call. The
row stays a warning when auth works, including after the policy is
attached, because OK would claim the policy was read back.
