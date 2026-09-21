# sec-10k-agent

Two-stage **SequentialAgent** over `knowledge_assistant_tool`. Stage 1 asks an
Agent Bricks Knowledge Assistant that already indexes 10-K filings; stage 2
rewrites the grounded answer into a short cited brief. Citations come from the
KA — this example does not ingest EDGAR or invent `doc_uri`s.

## What it does

A user asks a 10-K question ("What did Apple disclose about revenue risk in
FY2023?"). `sec_10k_research` must call `ask_knowledge_assistant` and pass
through the grounded answer plus citations. `sec_10k_brief` has no tools: it
rewrites that answer and lists every `doc_uri`. If the KA returns an error or
no citations, the brief fails closed instead of guessing.

OBO is already inside `knowledge_assistant_tool` via `UserClientDependency` —
per-user KA access policies apply. The only required config is
`APX_KA_ENDPOINT_NAME`.

## Prerequisites

- Databricks workspace with a configured CLI profile
- An Agent Bricks Knowledge Assistant serving endpoint that already indexes
  the 10-K corpus you want to query
- `CAN_QUERY` on that KA endpoint and on the LLM serving endpoint
- `APX_KA_ENDPOINT_NAME` set to the KA endpoint name (never hardcode it)

## Part 1: Workspace setup (one-time)

No one-time workspace setup is required in this repo. Point
`APX_KA_ENDPOINT_NAME` at a Knowledge Assistant that already exists in the
workspace. Building or ingesting the KA is out of scope for this example.

## Part 2: Local development

### Step 1: Install

```bash
uv sync
export APX_KA_ENDPOINT_NAME=<your-ka-endpoint>
```

### Step 2: Run the tests

The example's contract lives in the package suite (so it rides existing CI,
no extra job):

```bash
cd ../../ && uv run --frozen pytest tests/test_sec_10k_agent_example.py
```

### Step 3: Run locally

```bash
uv run uvicorn app:app --reload
```

Open `http://localhost:8000/_apx/agent` for the dev UI, or POST to `/responses`.

## Part 3: Deploy to Databricks Apps

### Step 1: Review `app.yml`

Replace `<your-ka-endpoint>` in `app.yml` and `databricks.yml` with the real
KA serving-endpoint name. The bundle declares `user_api_scopes:
[serving.serving-endpoints]` and `CAN_QUERY` on both the LLM and KA endpoints.

### Step 2: Deploy

```bash
databricks bundle deploy
```

### Step 3: Verify

Ask a filing-grounded question in the app UI. The brief must list `doc_uri`
citations from the KA. An unset `APX_KA_ENDPOINT_NAME` fails closed at
`get_agent()`.

## Configuration

| Variable | Default | What it controls |
|---|---|---|
| `APX_KA_ENDPOINT_NAME` | _(required)_ | Agent Bricks Knowledge Assistant serving-endpoint name |
| `AGENT_MODEL` | `databricks-meta-llama-3-3-70b-instruct` | LLM serving endpoint for both stages |
| `MLFLOW_EXPERIMENT_NAME` | `/Shared/sec-10k-agent` | MLflow experiment for traces |

## Tools

| Tool | Stage | What it returns |
|---|---|---|
| `ask_knowledge_assistant` | `sec_10k_research` | `{question, answer, citations:[{doc_uri}]}` or `{question, error}` |

## Project structure

```
sec-10k-agent/
├── agent.py          # SequentialAgent factory + get_agent()
├── app.py            # Local run entrypoint
├── app.yml           # Databricks Apps entrypoint + env
├── databricks.yml    # Bundle: OBO serving scopes + two CAN_QUERY endpoints
└── pyproject.toml
```
