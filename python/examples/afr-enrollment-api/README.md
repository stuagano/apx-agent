# AFR Enrollment API

A standalone Databricks App that provides a deterministic enrollment decision for AFR (Affordable Rate) applications. No LLM — pure normalize → search → evaluate → log pipeline. Designed for batch processing at high throughput.

## Prerequisites

| Requirement | Version / Notes |
|-------------|----------------|
| Python | 3.11+ |
| [uv](https://docs.astral.sh/uv/) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| apx-agent | Not yet on PyPI — clone this repo: `git clone https://github.com/stuagano/apx-agent` |
| Databricks workspace | Required for live mode; not needed for `DEMO_MODE=true` |

## What it does

`POST /api/enroll` accepts an AFR application (name, address, email, account number) and returns a structured enrollment decision.

**Decision categories:**

| Category | Confidence | Meaning |
|----------|-----------|---------|
| `EXACT` | ≥ 0.90 | Near-certain match |
| `HIGH_CONFIDENCE` | ≥ 0.75 | Strong match, approve |
| `LOW_CONFIDENCE` | < 0.75 | Review recommended |
| `NO_MATCH` | 0.0 | No candidate found |

## Quick start (demo mode)

```bash
git clone https://github.com/stuagano/apx-agent
cd apx-agent/python/examples/afr-enrollment-api
uv sync
DEMO_MODE=true uv run uvicorn afr_enrollment_api.backend.app:app --reload
```

Then:

```bash
curl -s -X POST http://localhost:8000/api/enroll \
  -H "Content-Type: application/json" \
  -d '{
    "applicant_name": "Jane Smith",
    "address": "123 Maple Ave",
    "account_number": "DEN-001234"
  }'
```

## Search strategy

By default, the enrollment API runs Vector Search + SQL locally. In production, set `SEARCH_SERVICE_URL` to delegate search to the dedicated `account-search-service` app — this lets search scale independently from the enrollment pipeline.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DEMO_MODE` | No | `"true"` → synthetic data, no live Databricks needed |
| `SEARCH_SERVICE_URL` | No | URL of account-search-service (recommended for production) |
| `VS_INDEX_*` + `UTILITY_ACCOUNT_TABLE` | When no `SEARCH_SERVICE_URL` | Local VS/SQL fallback |
| `AFR_DECISION_TABLE` | Yes (live) | Table to write enrollment decisions |

## Part of a 3-app architecture

```
account-search-service  ←  POST /api/search
        ↑
afr-enrollment-api      ←  POST /api/enroll  ← you are here
entity-resolution-agent ←  /api/chat (for ambiguous cases needing LLM reasoning)
```

For ambiguous edge cases (nicknames, maiden names, multi-account households) where the deterministic pipeline returns `LOW_CONFIDENCE`, route to the `entity-resolution-agent` chat interface for LLM-powered reasoning.
