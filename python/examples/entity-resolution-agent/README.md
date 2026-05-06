# Entity Resolution Agent

Fuzzy-match and resolve customer/account entities built on [apx-agent](https://github.com/stuagano/apx-agent) and Databricks — **in under 300 lines of Python**.

![Chat UI showing match result with trace panel](docs/screenshot-chat.png)

The agent normalizes an intake application, runs Vector Search (or SQL fallback for initials/acronyms), evaluates candidates with rule-based edge case detection, and logs every decision to a Delta table. The trace panel shows every tool call, input, and output in real time.

---

## What makes this simple

The entire orchestration is a single `LlmAgent` with five plain Python functions as tools:

```python
# agent_router.py — this is the whole agent
agent = LlmAgent(
    tools=[normalize_record, vector_search, sql_search, evaluate_candidates, log_decision],
    instructions=_INSTRUCTIONS,
    max_iterations=10,
)
```

Tools are just regular functions. Databricks auth is handled automatically via `Dependencies.Workspace`:

```python
def vector_search(query: str, k: int = 10, ws: Workspace = None) -> dict:
    """Search the VS index for candidate accounts matching the query."""
    index = ws.vector_search_indexes.query_index(
        index_name=os.environ["VECTOR_SEARCH_INDEX_NAME"],
        columns=["account_id", "name", "address"],
        query_text=query,
        num_results=k,
    )
    ...
```

No boilerplate. No auth wiring. `ws` is injected per-request with the caller's OBO token when running on Databricks Apps, or falls back to CLI credentials locally.

![Trace panel showing normalize → vector_search → evaluate pipeline](docs/screenshot-trace.png)

---

## Pipeline

```
"Match Jane Smith at 123 Maple Ave"
           │
           ▼
   normalize_record        → cleaned name/address, decides strategy (vector | sql)
           │
           ▼
   vector_search           → top-k candidates from Databricks Vector Search
   sql_search              → ILIKE fallback for initials (J. Smith) or acronyms (LLC)
           │
           ▼
   evaluate_candidates     → confidence score + EXACT/HIGH/LOW/NO_MATCH category
           │                  edge cases: familial match, nickname, account# boost
           ▼
   log_decision            → writes to Delta audit table, returns result to user
```

---

## Quick Start

```bash
uv sync

# Run in demo mode — no VS index or SQL warehouse needed
DEMO_MODE=true uv run uvicorn entity_resolution_agent.backend.app:app --port 8001
```

Open `http://localhost:8001` and try:

| Prompt | What it exercises |
|--------|-------------------|
| `Match Jane Smith at 123 Maple Ave` | Vector search → LOW_CONFIDENCE (common name, no account number) |
| `Match Jane Smith at 123 Maple Ave, account 9876` | Same + account number exact-match boost → HIGH_CONFIDENCE |
| `Match J. Smith at 567 Birch Lane` | Initials → SQL fallback path |
| `Match Liz Rodriguez at 456 Oak Street` | Nickname variant |
| `Match John Smith at 123 Maple Ave` | Familial match flagged (same address, different first name) |

```bash
uv run pytest tests/ -v
```

---

## Configuration

| Env var | Demo | Production |
|---------|------|------------|
| `DEMO_MODE` | `true` | `false` |
| `VECTOR_SEARCH_ENDPOINT_NAME` | _(ignored)_ | Your VS endpoint |
| `VECTOR_SEARCH_INDEX_NAME` | _(ignored)_ | `catalog.schema.account_idx` |
| `ACCOUNT_TABLE` | _(ignored)_ | `catalog.schema.accounts` |
| `DECISION_TABLE` | _(ignored)_ | `catalog.schema.match_decisions` |

---

## Project Structure

```
entity-resolution-agent/
├── src/entity_resolution_agent/backend/
│   ├── agent_router.py        # LlmAgent — 10 lines
│   ├── app.py                 # FastAPI app — 8 lines
│   ├── models.py              # Application, Candidate, MatchDecision
│   └── core/
│       ├── supervisor.py      # normalize_record, vector_search, sql_search
│       ├── evaluator.py       # evaluate_candidates, log_decision + edge case logic
│       └── demo_data.py       # 15 synthetic accounts for DEMO_MODE
└── tests/
    ├── test_supervisor_tools.py
    ├── test_evaluator_tools.py
    └── test_agent_wiring.py
```

---

## Edge Cases

| Case | Mechanism |
|------|-----------|
| Initials (`J. Smith`) | Regex → `strategy="sql"` → `sql_search` ILIKE |
| Acronyms (`ABC LLC`) | Same |
| Familial match | Same address + same surname + different first → flagged in rationale |
| Account number match | +0.05 confidence boost |
| No candidates | Tries alternate search tool before returning NO_MATCH |
| LOW_CONFIDENCE | Category returned, manual review recommended |
