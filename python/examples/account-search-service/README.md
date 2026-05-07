# Account Search Service

A standalone Databricks App that exposes utility account search as an HTTP API. Designed to be deployed independently and called by other services — the AFR enrollment pipeline, the entity-resolution LLM agent, or any other service that needs fuzzy account lookup.

## What it does

`POST /api/search` normalizes a name+address, selects the right search strategy, fans out across three Vector Search indexes, and returns deduplicated candidates.

| Index | Embedding column | Catches |
|-------|-----------------|---------|
| `_full_idx` | `first_name last_name address` | Standard name matches |
| `_last_addr_idx` | `last_name address` | Familial / spouse matches |
| `_first_email_idx` | `first_name email` | Maiden name matches |

Names with initials (J. Smith) or acronyms (ABC LLC) bypass Vector Search and use SQL ILIKE instead — these embed poorly under cosine distance.

## Quick start (demo mode)

```bash
cd account-search-service
uv sync
DEMO_MODE=true uv run uvicorn account_search_service.backend.app:app --reload
```

Then:

```bash
curl -s -X POST http://localhost:8000/api/search \
  -H "Content-Type: application/json" \
  -d '{"applicant_name": "Jane Smith", "address": "123 Maple Ave"}'
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEMO_MODE` | No | `"true"` → synthetic data, no live Databricks needed |
| `VS_ENDPOINT` | Yes (live) | Vector Search endpoint name |
| `VS_INDEX_FULL` | Yes (live) | `catalog.schema.utility_account_entities_full_idx` |
| `VS_INDEX_LAST_ADDR` | Yes (live) | `catalog.schema.utility_account_entities_last_addr_idx` |
| `VS_INDEX_FIRST_EMAIL` | Yes (live) | `catalog.schema.utility_account_entities_first_email_idx` |
| `UTILITY_ACCOUNT_TABLE` | Yes (SQL path) | `catalog.schema.utility_account_entities` |

## Part of a 3-app architecture

This service is one of three apps in the entity resolution system:

```
account-search-service  ←  POST /api/search
        ↑
afr-enrollment-api      ←  POST /api/enroll (calls search service + evaluate + log)
entity-resolution-agent ←  /api/chat (LLM HandoffAgent, Supervisor calls search service)
```

See [`../entity-resolution-agent/README.md`](../entity-resolution-agent/README.md) for the full architecture and Vector Search index setup guide.
