# Reference Examples — Design Spec

## Goal

Port the working Uplight agents into the public `apx-agent` repository as clean, generic reference examples. Remove all Uplight-specific branding, URLs, and workspace identifiers. Replace the existing `hub/` with the more capable uplight-agent-hub (stripped of Uplight branding).

## Scope

### What's being added / replaced

| Location | Change | Source |
|---|---|---|
| `python/examples/data-triage-agent/` | New example | `uplight/agents/data-triage-agent` |
| `python/examples/data-inspector/` | New example | `uplight/agents/data-inspector` |
| `python/examples/contract-parsing-agent/` | New example | `uplight/agents/contract-parsing-agent` |
| `typescript/examples/data-triage-agent/` | New example | `uplight/agents/data-triage-agent-ts` |
| `hub/` | Replace entirely | `uplight/agents/uplight-agent-hub` |

### What's not changing

- `python/examples/explain_my_bill_agent/` — already in repo, unchanged
- `python/examples/voynich/` — unchanged
- `typescript/examples/basic-agent/` — unchanged
- `typescript/examples/pipeline-agent/` — unchanged
- `typescript/examples/voynich/` — unchanged
- All SDK source code (`python/src/`, `typescript/src/`) — unchanged

---

## Hub Replacement

### What changes from uplight-agent-hub

**Branding / naming:**
- Package name: `uplight_agent_hub` → `agent_hub`
- App name: `uplight-agent-hub` → `agent-hub`
- Display title: "Uplight Agent Hub" → "Agent Hub"
- All `uplight_agent_hub` import paths updated throughout

**`AgentCard` model:**
- Drop `workstream: Optional[str]` field — it's a Uplight UCO concept with no meaning outside that context
- Keep all other fields: `id`, `name`, `display_name`, `description`, `status`, `url`, `tags`, `supports_invoke`, `tools`, `last_seen`

**Seeded registry (`router.py`):**
Replace the 6 hardcoded Uplight agents with 3 generic placeholders that demonstrate the pattern:
- One `live` agent pointing to `os.environ.get("EXAMPLE_AGENT_URL")` — shows how to wire a real deployed agent
- One `stub` agent — shows how to pre-seed a planned-but-not-deployed agent
- One agent with `status="unreachable"` — shows what an agent looks like when its URL is down

**URLs / env vars:**
- Remove all `*.databricksapps.com` hardcoded URLs
- `AUTO_REGISTER_URLS` list in startup: replace Uplight URLs with `os.environ.get("AGENT_HUB_AGENT_URLS", "").split(",")` so operators can inject URLs at deploy time
- `app.yml`: replace Uplight-specific env var values with empty/placeholder values

**README:** New `hub/README.md` covering:
- What the hub is and when to use it
- How to register an agent (seeded vs. auto-register vs. discovery crawl)
- Required env vars
- How to deploy to Databricks Apps

### What stays the same

- Two-panel chat UI (full `ChatPanel.tsx`, `AgentListItem`, `StatusDot`)
- `/api/agents/{id}/invoke` proxy with forwarded auth token
- `supports_invoke` flag and gating
- Auto-registration on startup
- React + TypeScript + TanStack Router + TanStack Query + Tailwind + Vite frontend
- FastAPI backend structure

---

## Individual Agents

### Sanitization approach: surface + light genericization

Each agent gets:
1. All hardcoded `*.databricksapps.com` URLs → env vars with no default (fail-fast at startup if missing)
2. Any Uplight-specific catalog/schema/table names → `${CATALOG}.${SCHEMA}.table_name` pattern in system prompts and tool descriptions
3. A `README.md` with: what the agent does, required env vars, deploy steps

### data-triage-agent (Python)

**Changes:**
- `app.py`: remove hardcoded `DATA_INSPECTOR_URL` default; `AGENT_HUB_URL` stays as optional registration target
- System prompt: replace any literal UC table names with generic `<catalog>.<schema>.<table>` examples
- Tool implementations: already generic (SQL queries, lineage API calls, job API calls) — no changes needed
- `app.yml`: replace Uplight `DATA_INSPECTOR_URL` value with empty placeholder
- `pyproject.toml`: update `[tool.apx.agent]` url to empty string; update any Uplight references in description

**What stays:** All 11 tools (`run_sql_query`, `get_table_info`, `get_table_lineage`, `find_jobs_for_table`, `get_job_run_history`, `get_job_run_logs`, `get_job_source_paths`, `list_genie_spaces`, `query_genie_space`, `read_github_file`, `search_github_code`) — these are genuinely generic Databricks tools.

### data-triage-agent (TypeScript)

**Changes:**
- `app.ts`: replace hardcoded workspace URLs with `process.env.*`
- `databricks.yml`: remove Uplight workspace ID from `host:` field — replace with `${DATABRICKS_HOST}`
- `deploy.sh` (if present): remove Uplight-specific `--profile` defaults
- `app.yaml`: same as Python version — empty placeholder values

**What stays:** All tool implementations, esbuild config pattern, the full TypeScript agent structure.

### data-inspector

**Changes:**
- Remove any hardcoded catalog/schema references → env vars
- `app.yml` / `pyproject.toml`: update URLs and names
- Add `README.md`

**What stays:** SQL query tool, Delta forensics tools — all generic.

### contract-parsing-agent

**Changes:**
- Replace hardcoded UC catalog path (e.g., `main.uplight_contracts.extracted`) → env vars `CONTRACT_CATALOG`, `CONTRACT_SCHEMA`
- Replace vector search index name → env var `CONTRACT_VECTOR_INDEX`
- System prompt: replace Uplight-specific contract terminology with generic utility contract terminology
- `pyproject.toml`: update agent name/description

**What stays:** The full extraction pipeline, tool implementations, the upload endpoint — the pattern is the point.

---

## README Convention

Each example gets a `README.md` with this structure:

```markdown
# <Agent Name>

One-sentence description.

## What it does

2-3 sentences on the use case and what tools it exposes.

## Required env vars

| Variable | Description |
|---|---|
| `VAR_NAME` | What it's for |

## Deploy to Databricks Apps

<deploy steps>
```

---

## What Doesn't Change

- No new features added to any agent
- No test suites added (examples ship without tests — the build passing is the bar)
- No changes to SDK internals
- The `explain_my_bill_agent` example already in the repo is not touched
