# Entity Resolution Agent

Fuzzy-match and resolve customer/account entities using a two-agent `HandoffAgent` pipeline built on [apx-agent](https://github.com/stuagano/apx-agent) and Databricks.

Demonstrates matching intake applications against a customer master database using `HandoffAgent` with a Supervisor → Evaluator loop, Vector Search as the primary search tool, and SQL fallback for records with initials or acronyms.

## Architecture

```
intake application
      │
      ▼
┌─────────────┐   vector_search (default)   ┌──────────────────────┐
│  SUPERVISOR │ ──────────────────────────▶ │  Databricks          │
│  (fast)     │   sql_search (initials/     │  Vector Search / SQL │
│             │   acronyms fallback)        └──────────────────────┘
│  normalize  │◀──── candidates shortlist ──────────────────────────┘
│  + search   │
└──────┬──────┘
       │  application + shortlist
       ▼
┌──────────────┐
│  EVALUATOR   │  fuzzy reasoning, edge cases
│  (smart)     │  (familial matches, nicknames,
│              │   secondary addresses)
│              │──▶ match decision + log
└──────┬───────┘
       │ low confidence?
       └──▶ transfer_to_supervisor (retry with hints)
```

**Key apx-agent patterns demonstrated:**
- `HandoffAgent` — two-agent loop with mid-conversation handoffs
- `LlmAgent` — tool-calling agents with injected `WorkspaceClient`
- Vector Search via `ws.vector_search_indexes.query_index`
- SQL fallback with injection-safe token escaping
- `DEMO_MODE` — fully runnable without real infrastructure

## Quick Start

```bash
# Install
uv sync

# Run in demo mode (no real VS index or SQL warehouse needed)
DEMO_MODE=true uv run uvicorn entity_resolution_agent.backend.app:app --port 8001

# Run tests
uv run pytest tests/ -v
```

Then open `http://localhost:8001` and try prompts like:

- `"Match Jane Smith at 123 Maple Ave"` — high-confidence EXACT match
- `"Match J. Smith at 567 Birch Lane"` — initials trigger SQL fallback path
- `"Match Liz Rodriguez at 456 Oak Street"` — nickname variant, LOW_CONFIDENCE
- `"Match John Smith at 123 Maple Ave"` — familial match flagged (same address/surname as Jane)

## Configuration

| Env Var | Demo default | Production value |
|---------|-------------|-----------------|
| `DEMO_MODE` | `true` | `false` |
| `VECTOR_SEARCH_ENDPOINT_NAME` | _(ignored in demo)_ | Your VS endpoint name |
| `VECTOR_SEARCH_INDEX_NAME` | _(ignored in demo)_ | `catalog.schema.account_idx` |
| `ACCOUNT_TABLE` | _(ignored in demo)_ | `catalog.schema.accounts` |
| `DECISION_TABLE` | _(ignored in demo)_ | `catalog.schema.match_decisions` |

## Project Structure

```
entity-resolution-agent/
├── src/entity_resolution_agent/
│   └── backend/
│       ├── agent_router.py        # HandoffAgent wiring (supervisor + evaluator)
│       ├── app.py                 # FastAPI app
│       ├── models.py              # Application, Candidate, MatchDecision
│       └── core/
│           ├── supervisor.py      # normalize_record, vector_search, sql_search
│           ├── evaluator.py       # evaluate_candidates, log_decision
│           └── demo_data.py       # 15 synthetic accounts for DEMO_MODE
└── tests/
    ├── conftest.py
    ├── test_supervisor_tools.py
    ├── test_evaluator_tools.py
    └── test_agent_wiring.py
```

## Edge Cases Handled

| Case | How it's handled |
|------|-----------------|
| Initials (`J. Smith`) | `_INITIAL_RE` regex → `strategy="sql"` → `sql_search` |
| Acronyms (`ABC LLC`) | `_ACRONYM_RE` regex → `strategy="sql"` → `sql_search` |
| Familial match (same address, same surname, different first) | Evaluator notes in rationale |
| Account number exact match | +0.05 confidence boost |
| Low confidence (< 0.70) | `NO_MATCH` category; evaluator hands back to supervisor with hints |
| No candidates found | Supervisor tries alternate search tool before giving up |
