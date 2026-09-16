# Route an agent's model through Bedrock behind Mosaic AI Gateway

Run your agent's LLM calls on **Amazon Bedrock** — so inference bills to your
AWS account and burns down AWS committed spend — while keeping every **Mosaic
AI Gateway** benefit (guardrails, rate limits, usage tracking, payload logging,
fallbacks, UC `CAN_QUERY` permissions).

The trick: an agent's model is just a **serving-endpoint name**
(`[tool.apx.agent] model = "..."`). apx-agent needs no code change. You create
a Databricks *external-model* endpoint whose backend is Bedrock, attach AI
Gateway to that endpoint, and point the agent at its name.

```
apx-agent (model = "bedrock-claude")   ← pyproject / databricks.yml, no code change
        │
        ▼
Databricks serving endpoint  ← Mosaic AI Gateway attaches HERE
  external_model → amazon-bedrock       (guardrails, rate limits,
                                         usage + inference logging, fallbacks)
        │
        ▼
Amazon Bedrock   ← tokens billed by AWS to your account (commit burn-down)
```

**Billing:** external-model endpoints don't charge per-token model DBUs — the
provider (AWS/Bedrock) invoices you directly. Only the small serving-proxy
overhead is Databricks-side, not per token. See
[cost-tracking.md](cost-tracking.md) for how apx surfaces the rest.

## 1. Store the Bedrock credentials

Pick **one** auth method for `amazon_bedrock_config`, most-governed first:

| Method | Field | Notes |
|---|---|---|
| **UC service credential** (recommended) | `uc_service_credential_name` | Bedrock access governed by Unity Catalog — most on-brand; no long-lived keys. |
| Instance profile | `instance_profile_arn` | IAM role assumed by the endpoint. |
| Access keys | `aws_access_key_id` / `aws_secret_access_key` | Reference secrets, never plaintext: `{{secrets/<scope>/<key>}}`. |

For access keys:

```bash
databricks secrets create-scope bedrock --profile <PROFILE>
databricks secrets put-secret bedrock aws_access_key_id     --profile <PROFILE>
databricks secrets put-secret bedrock aws_secret_access_key --profile <PROFILE>
```

## 2. Create the external-model endpoint (with AI Gateway)

Save as `bedrock-endpoint.json` and create it. `bedrock_provider` is the model
family on Bedrock (`Anthropic`, `Amazon`, `Cohere`, `AI21Labs` — case-insensitive);
`external_model.name` is the Bedrock model id.

```json
{
  "config": {
    "served_entities": [
      {
        "name": "bedrock-claude",
        "external_model": {
          "name": "anthropic.claude-3-5-sonnet-20241022-v2:0",
          "provider": "amazon-bedrock",
          "task": "llm/v1/chat",
          "amazon_bedrock_config": {
            "aws_region": "us-east-1",
            "bedrock_provider": "Anthropic",
            "uc_service_credential_name": "bedrock_cred"
          }
        }
      }
    ]
  },
  "ai_gateway": {
    "usage_tracking_config": { "enabled": true },
    "inference_table_config": {
      "enabled": true,
      "catalog_name": "main",
      "schema_name": "ai_gateway",
      "table_name_prefix": "bedrock_claude"
    },
    "guardrails": {
      "input":  { "safety": true, "pii": { "behavior": "BLOCK" } },
      "output": { "pii": { "behavior": "BLOCK" } }
    },
    "rate_limits": [
      { "calls": 60, "renewal_period": "minute", "key": "endpoint" }
    ]
  }
}
```

> **CLI-version gotcha:** the typed `serving-endpoints create` strips fields its
> bundled SDK doesn't know — CLI v1.12.1 silently drops `uc_service_credential_name`
> (creating the endpoint with *no* auth). If you hit
> `Warning: unknown field: uc_service_credential_name`, POST the raw JSON instead:
> `databricks api post /api/2.0/serving-endpoints --json @bedrock-endpoint.json --profile <PROFILE>`.
> Also: `bedrock_provider` must be lowercase (`anthropic`, not `Anthropic`), and
> `name` goes *inside* the JSON, not as a positional arg.

```bash
databricks serving-endpoints create bedrock-claude \
  --json @bedrock-endpoint.json --profile <PROFILE>

# wait until ready:
databricks serving-endpoints get bedrock-claude --profile <PROFILE> \
  | jq '{ready: .state.ready, config_update: .state.config_update}'
# ready when ready == "READY" AND config_update == "NOT_UPDATING"
```

Swap access-key auth in by replacing the `uc_service_credential_name` line with:

```json
"aws_access_key_id": "{{secrets/bedrock/aws_access_key_id}}",
"aws_secret_access_key": "{{secrets/bedrock/aws_secret_access_key}}"
```

Change AI Gateway config later without recreating the endpoint:
`databricks serving-endpoints put-ai-gateway bedrock-claude --json @gateway.json`.

## 3. Point the agent at it

`pyproject.toml`:

```toml
[tool.apx.agent]
model = "bedrock-claude"          # was databricks-claude-sonnet-4-6
```

`databricks.yml` — set the variable + declare the `serving_endpoint` resource
with `CAN_QUERY` so the platform mints the OBO-scoped token (same wiring as any
FM endpoint):

```yaml
variables:
  llm_endpoint_name:
    default: bedrock-claude       # was databricks-claude-sonnet-4-6
```

The existing `llm-endpoint` resource block already grants `CAN_QUERY` against
`${var.llm_endpoint_name}` and passes it as `APX_MODEL` — nothing else to add.

After deploy, `apx-agent doctor` reads `ai_gateway.guardrails` on this endpoint,
so its guardrail check now **passes** instead of warning.

## Caveats (honest)

- **Model auth is the endpoint's, not per-user.** apx-agent's OBO identity
  passthrough governs *tools and data* under the caller's UC grants. The Bedrock
  call runs under the endpoint's configured credential (a shared principal) —
  inherent to every external-model endpoint. Per-user governance stays on the
  tool/data side. See [../safety/identity-passthrough.md](../safety/identity-passthrough.md).
- **Region / residency:** prompts and responses transit the Gateway → your
  chosen Bedrock region. Confirm that against any data-residency constraints.
- **Feature matrix:** most AI Gateway features work on external models; confirm
  the specific ones you depend on for your workspace/region against the
  [external models docs](https://docs.databricks.com/aws/en/machine-learning/foundation-models/external-models),
  and inspect the live contract with
  `databricks serving-endpoints get-open-api bedrock-claude`.
- **Bedrock guardrails passthrough:** the Gateway can forward
  `X-Amzn-Bedrock-Guardrail*` headers if you also use native Bedrock guardrails —
  independent of the Gateway guardrails above.
