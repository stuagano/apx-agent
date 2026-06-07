# Getting started

An **agent** is a declaration: `instructions` + `tools` + a model name. apx-agent compiles it to whichever Databricks runtime you target without changing the agent definition.

## Prerequisites

**Python 3.11 or higher is required.** Check before continuing:

```bash
python --version   # or python3 --version
```

```bash
# 1. uv — Python package and project manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Databricks CLI v2. Older docs reference `pip install databricks-cli`;
#    that's the legacy v0.17 Python CLI and won't work here.
brew tap databricks/tap && brew install databricks
#   (or:  curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh)

# 3. Authenticate against your workspace.
databricks auth login --host https://<workspace>.cloud.databricks.com --profile <name>
databricks current-user me --profile <name>   # must succeed before apx-agent run

# 4. If you have multiple profiles, tell apx which one to use.
export DATABRICKS_CONFIG_PROFILE=<name>
```

**Workspace requirements:**

- A model serving endpoint accessible to your user (e.g. `databricks-claude-sonnet-4-6`). Confirm in your workspace under **Serving**.
- A SQL warehouse (Classic, Pro, or Serverless) — needed for `DataAgent` queries.
- **Databricks Apps must be enabled** in your workspace. Check under **Workspace Settings → Apps**. Without this, `apx-agent deploy` will fail. If Apps isn't available, ask your workspace admin or use `--target model-serving` instead.

### Install apx-agent

```bash
uv add apx-agent
```

Or pin a specific version: `uv add "apx-agent==0.3.0"`. To track `main`
before a release: `uv add "apx-agent @ git+https://github.com/stuagano/apx-agent.git@main#subdirectory=python"`.

### Verify setup

Once `apx-agent` is installed, run `apx-agent doctor` to confirm everything is wired before you touch `apx-agent run` or `apx-agent deploy`:

```bash
uv run apx-agent doctor            # checks Python, uv, Databricks CLI, auth, project layout
uv run apx-agent doctor --offline  # skip the live workspace round-trip (CI / offline)
```

It prints a `Fix:` line for anything wrong. Auth errors caught here are much cleaner than errors mid-`apx-agent run`.

---

## Scaffold and run locally

```bash
uv add apx-agent                 # install apx into your current env
uv run apx-agent scaffold my-agent     # creates my-agent/ in the current directory
cd my-agent && uv sync           # my-agent has its own isolated env — sync it
```

> `apx-agent scaffold` is interactive by default — it asks for a catalog, schema, and
> SQL warehouse, then bakes the schema into `.apx/schema.json` so the agent is
> grounded before the first question. To skip the prompts and use defaults:
> `uv run apx-agent scaffold my-agent --no-interactive`.

> `apx-agent scaffold` creates the project in the current directory. If you want it
> somewhere else, `cd` there first.

### What scaffold gave you

```
my-agent/
├── agent.py                  ← the one file you edit
├── pyproject.toml            ← deps + [tool.apx.agent] config
├── databricks.yml            ← Databricks bundle (used by `apx-agent deploy`)
├── .env.example              ← copy to .env for local secrets
├── .gitignore
├── README.md
├── agent_server/
│   ├── __init__.py
│   └── start_server.py       ← framework boilerplate — don't edit
└── scripts/
    ├── __init__.py
    └── quickstart.py         ← `uv run quickstart` creates the MLflow experiment
```

There are **two surfaces** for this agent and you'll bounce between them:

- **Your IDE** — open `agent.py`. This is the source of truth. Add tools, change instructions, swap the model. Save the file and the dev server reloads.
- **The browser at `http://localhost:8000`** — chat with the agent, see traces, invoke the setup wizard. Every view lives under `/_apx/*` and is just a window onto the same `agent.py`.

### Run it

```bash
uv run apx-agent run --reload
```

This starts FastAPI on `:8000` with file-watch reload. Leave it running in one terminal; edit `agent.py` in your IDE in the other.

### What you should see (walk this end-to-end)

The scaffolded agent is already grounded against `samples.nyctaxi` — it knows the tables and columns and can query without any setup. Walk through this before deploying:

1. **Open `http://localhost:8000`.** It redirects to `/_apx/agent` — a tabbed shell with **Chat · Edit · Eval · Probe** tabs and **⧉ Topology** / **⏱ Traces** header buttons.
2. **On the Chat tab**, ask **"what tables can you query?"** then **"how many taxi trips are in the data?"**. The agent picks the SQL tool, runs the query as you (your OAuth token, your UC grants), and streams the answer with the tool call shown inline.
3. **Click the ⏱ Traces button.** A new tab opens at `/_apx/traces` listing recent turns. Click a row to drill into the span tree — LLM call, tool call, SQL execution, response. Production traces look identical, just stored in your workspace's MLflow experiment.
4. **Open `agent.py` in your IDE.** Change `instructions=` to something specific to your data, save. The reload kicks in; ask another question and watch the new behavior. This is the loop.

**To connect your own data:** Click **Open Setup** in the chat panel (or go to `/_apx/setup`) — pick a catalog, schema, and SQL warehouse. Setup writes the values to your project's `.env` and can optionally rewrite `agent.py`'s instructions grounded in the real columns.

If a SQL query returns nothing, the warehouse is probably stopped — open the **Probe** tab at `/_apx/probe/checks` to confirm and click through to start it.

When the agent looks right locally, ship it.

---

## Deploy to Databricks Apps

```bash
uv run apx-agent deploy
```

From inside `my-agent/`, this bundles the project and creates a Databricks App. `apx-agent deploy` prints the app URL when it finishes.

(For Model Serving instead: `apx-agent deploy --target model-serving --name <catalog.schema.model>`.)

---

## After deploy — confirming it works

`apx-agent deploy` prints the app URL when it finishes. To confirm the agent is live:

1. **Check `/readyz`** — `curl <app-url>/readyz` returns `{"status":"ok"}` when the agent is running and all capabilities (memory, warehouse) are healthy. This is the fastest smoke test.
2. **Ask a question** — open `<app-url>` in a browser and ask the same questions you tested locally. If the agent answers, the deploy is healthy.
3. **Check traces** — open your workspace, go to **Machine Learning → Experiments**, and find your agent's experiment. A trace with a successful tool call confirms the full stack (auth, SQL warehouse, UC) is working end-to-end.

If the app shows a 502 or `/readyz` returns an error, run `uv run apx-agent doctor` locally and check [`docs/deploy/troubleshooting.md`](../deploy/troubleshooting.md).

---

## The agent file

`agent.py` is the only file you edit. The scaffolded version is a one-line **`DataAgent`** — a governed agent over a Unity Catalog schema:

```python
from apx_agent import DataAgent

agent = DataAgent("samples", "nyctaxi")
```

Point it at your own data by changing the catalog/schema. The scaffolded agent
already has the schema baked in (no runtime discovery needed). To refresh
the schema after table changes, re-run `apx-agent scaffold`.

`DataAgent` is just a specialized `Agent`, so you can also wire tools directly:

| Tool | What it wraps |
|------|---------------|
| `uc_function_tool("catalog.schema.fn")` | A Unity Catalog SQL function |
| `genie_tool("space_id")` | A Genie space — natural-language SQL over any data |
| `vector_search_tool("index_name")` | A Databricks Vector Search index |
| `@tool def fn(...) -> str` | Any Python function |

### Want an agent that joins two source systems?

`CoworkerAgent` is a `DataAgent` for the two-system join pattern: two source
systems landed in one UC schema, joined on a business entity, answering a
question neither system can answer alone.

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
    # memory="persistent",  # uncomment to remember facts across sessions
)
```

Scaffold one with `apx-agent scaffold my-coworker --template coworker`. See
[`docs/agents/coworker.md`](../agents/coworker.md) for the full reference.

---

## Updating your agent

After the initial deploy, `apx-agent deploy` uploads your code to a path in your Databricks workspace. To update without redeploying from the CLI:

1. Go to your workspace → **Apps** → select your app
2. Click **Edit source** to open `agent.py` in the workspace editor
3. Make your changes and save
4. Click **Restart** — the app stops, re-reads `agent.py`, and starts fresh at the same endpoint

No new deployment, no URL change.

---

## What's next

| Goal | Doc |
|------|-----|
| Join two source systems | [coworker.md](../agents/coworker.md) |
| Choose Apps vs Model Serving | [apps-vs-model-serving.md](../deploy/apps-vs-model-serving.md) |
| Build a multi-step pipeline | [composition.md](../agents/composition.md) |
| Add multi-turn memory | [sessions-and-memory.md](../running/sessions-and-memory.md) |
| Write and publish UC function tools | [tools/overview.md](../tools/overview.md) |
| DataAgent reference | [data-agent.md](../agents/data-agent.md) |
| pyproject.toml reference | [pyproject-toml.md](../reference/pyproject-toml.md) |
| Add compliance guards | [compliance.md](../safety/compliance.md) |
| Full CLI reference | [cli.md](cli.md) |

---

## Troubleshooting

`apx-agent doctor` is the first thing to run when something isn't working:

```bash
uv run apx-agent doctor            # full check incl. a live workspace round-trip
uv run apx-agent doctor --offline  # skip the network check (CI / offline)
uv run apx-agent doctor --json     # machine-readable output
```

**Auth errors** (`Could not resolve Databricks authentication`) — re-run `databricks auth login --profile <name>` and confirm with `databricks current-user me --profile <name>`, then restart `apx-agent run`.

**Warehouse stopped** — open the **Probe** tab at `/_apx/probe/checks`; it shows warehouse status and a link to start it.

**`apx-agent deploy` fails with Apps error** — confirm Apps is enabled in your workspace under **Workspace Settings → Apps**. If not available, use `--target model-serving` instead.

For deploy failures, see [`docs/deploy/troubleshooting.md`](../deploy/troubleshooting.md).
