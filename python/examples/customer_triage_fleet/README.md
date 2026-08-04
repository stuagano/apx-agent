# customer_triage_fleet

A fleet of 4 independently deployed apx-agent apps demonstrating **cross-app multi-agent composition over A2A**.

```
orchestrator  ─── A2A ──→  billing_specialist
              ─── A2A ──→  technical_specialist
              ─── A2A ──→  account_specialist
```

The **orchestrator** classifies customer queries and delegates to the right specialist via `sub_agents=[url]`. Each specialist is a standalone app serving its own A2A card, `/invocations`, and `/mcp` — independently deployable, discoverable, and governed per hop (the caller's OBO token passes through).

This is the "fleet" version of [`../customer_triage`](../customer_triage/), which composes the same specialists locally using `RouterAgent`. Use the fleet version when specialists have different consumers, deploy cadences, or scaling profiles.

## Quick start (local, smoke mode)

Run all 4 agents locally in separate terminals:

```bash
# Terminal 1: billing specialist
cd billing_specialist
APX_SMOKE_MODE=1 uv run uvicorn app:app --port 8001

# Terminal 2: technical specialist
cd technical_specialist
APX_SMOKE_MODE=1 uv run uvicorn app:app --port 8002

# Terminal 3: account specialist
cd account_specialist
APX_SMOKE_MODE=1 uv run uvicorn app:app --port 8003

# Terminal 4: orchestrator (discovers specialists via their A2A cards)
cd orchestrator
BILLING_SPECIALIST_URL=http://localhost:8001 \
TECHNICAL_SPECIALIST_URL=http://localhost:8002 \
ACCOUNT_SPECIALIST_URL=http://localhost:8003 \
APX_SMOKE_MODE=1 uv run uvicorn app:app --port 8000
```

Then send a request:

```bash
curl -X POST http://localhost:8000/invocations \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"I have a billing question about my last charge"}]}'
```

The orchestrator calls `classify_intent` → gets "billing" → delegates to the billing specialist via A2A.

## Verify specialist cards

Each specialist advertises its capabilities:

```bash
curl http://localhost:8001/.well-known/agent.json | jq .name
# "billing_specialist"

curl http://localhost:8002/.well-known/agent.json | jq .skills
# [{"name": "docs_search", ...}]
```

## Deploy to Databricks Apps

Each app deploys independently:

```bash
cd billing_specialist && databricks bundle deploy --target dev
cd technical_specialist && databricks bundle deploy --target dev
cd account_specialist && databricks bundle deploy --target dev

# Once specialists are deployed, grab their URLs and deploy the orchestrator:
cd orchestrator && databricks bundle deploy --target dev \
  --var "billing_specialist_url=https://billing-specialist.your-workspace.databricksapps.com" \
  --var "technical_specialist_url=https://technical-specialist.your-workspace.databricksapps.com" \
  --var "account_specialist_url=https://account-specialist.your-workspace.databricksapps.com"
```

## Architecture

| Component | Role | Key tools |
|-----------|------|-----------|
| `orchestrator/` | Triage + route | `classify_intent` (local), 3 remote delegates (auto-materialized from cards) |
| `billing_specialist/` | Billing domain | `get_recent_orders` |
| `technical_specialist/` | Technical domain | `docs_search` (Vector Search in prod) |
| `account_specialist/` | Account domain | `recall`, `remember` (semantic memory), `ask_account_data` (Genie) |

## Compared to `customer_triage` (single-app)

| | `customer_triage` | `customer_triage_fleet` |
|---|---|---|
| Composition | `RouterAgent` — local, in-process | `sub_agents=[url]` — remote, over A2A |
| Deploy unit | 1 app | 4 apps |
| Specialist discovery | Compile-time | Runtime (A2A cards at `/.well-known/agent.json`) |
| Independent scaling | No | Yes |
| Second consumer support | No (specialists are internal) | Yes (any agent can call a specialist) |
| Identity passthrough | Implicit (same process) | Explicit (OBO token forwarded per hop) |
