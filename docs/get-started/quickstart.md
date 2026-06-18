# Quickstart

In this guide you'll install apx-agent, scaffold a YAML spec grounded in a Unity Catalog schema, and deploy it to Databricks Apps.

**What you'll build:** a governed data agent that queries your Unity Catalog schema, runs every query as the calling user's identity, and streams answers through a built-in chat UI. You'll deploy it to Databricks Apps in the final step.

**Time:** 10–15 minutes for a first-time setup.

---

> **Coming from Google ADK or OpenAI Agents SDK?**
> `LlmAgent` (aliased as `Agent`) is your `Agent`. `DataAgent` is a specialized `Agent` grounded in a real UC schema. The scaffold below writes that intent into YAML; deploy generates the Apps project from it. See [migration.md](migration.md) for a full concept-mapping table.

---

## Prerequisites

**Python 3.11 or higher is required.** Check before continuing:

```bash
python --version   # or python3 --version
```

### Step 1 — Install tools

```bash
# uv — Python package and project manager
curl -LsSf https://astral.sh/uv/install.sh | sh

# Databricks CLI v2 (not the legacy pip-installable databricks-cli)
brew tap databricks/tap && brew install databricks
# or: curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
```

### Step 2 — Authenticate

```bash
databricks auth login --host https://<workspace>.cloud.databricks.com --profile <name>
databricks current-user me --profile <name>   # must succeed before apx-agent agents run
```

If you have multiple profiles:

```bash
export DATABRICKS_CONFIG_PROFILE=<name>
```

### Step 3 — Check workspace requirements

Before continuing, confirm your workspace has:

- A model serving endpoint accessible to your user (e.g. `databricks-claude-sonnet-4-6`). Check under **Serving**.
- A SQL warehouse (Classic, Pro, or Serverless) — needed for `DataAgent` queries.
- Databricks Apps enabled — check under **Workspace Settings → Apps**. Without this, `apx-agent agents deploy` will fail. If Apps isn't available, you can use `--target model-serving` instead.

### Step 4 — Install apx-agent

**As a global CLI tool** (recommended — makes `apx-agent` available from any terminal):

```bash
uv tool install apx-agent
```

This adds `apx-agent` to your PATH so you can run `apx-agent agents list`, `apx-agent doctor`, etc. from anywhere without activating a venv.

> **Already have a project?** Also add it as a project dependency so your `uv.lock` pins the exact version:
> ```bash
> uv add apx-agent
> ```

To pin a specific version: `uv tool install "apx-agent==0.3.4"`. To track `main`: `uv tool install "apx-agent @ git+https://github.com/stuagano/apx-agent.git@main#subdirectory=python"`.

> **Using `uv run` instead?** All commands in this guide also work as `uv run apx-agent <cmd>` from inside your project directory — `uv run` picks up the project's venv automatically.

### Verify your setup

```bash
uv run apx-agent doctor            # checks Python, uv, Databricks CLI, auth, project layout
uv run apx-agent doctor --offline  # skip the live workspace round-trip (CI / offline)
```

`apx-agent doctor` prints a `Fix:` line for anything wrong. Auth errors caught here are cleaner than errors mid-run.

---

## Scaffold and deploy

### Step 5 — Scaffold a YAML spec

```bash
uv run apx-agent agents scaffold my-agent
```

By default, scaffold writes `my-agent.yaml` in the current directory and does not create `my-agent/`. It records the agent name, model, instructions, tools, guardrails, and the `data` template with catalog/schema placeholders when you don't pass them.

> **Skip interactive prompts:** `uv run apx-agent agents scaffold my-agent --no-interactive` uses defaults. You can reconfigure later by editing the YAML or re-running scaffold.

> **Spec location:** scaffold writes the YAML in the current directory. `cd` to your preferred parent directory first.

### What scaffold gave you

```yaml
name: my-agent
description: ""
model: databricks-claude-sonnet-4-6
instructions: ""
examples: []
template:
  name: data
  catalog: $CATALOG
  schema: $SCHEMA
guardrails:
  injection_detection: false
tools: []
```

That's a deployable spec. Fill in `$CATALOG` and `$SCHEMA` (or pass `--catalog` / `--schema` when scaffolding) before deploying.

### Step 6 — Deploy to Databricks Apps

```bash
uv run apx-agent agents deploy my-agent.yaml --target apps
```

`deploy` reads `my-agent.yaml`, generates a Databricks Apps project in a temporary directory, bundles it, creates the App, and prints the URL when it finishes.

> **Deploy to Model Serving instead:** generate a project directory with `--no-yaml`, then run `uv run apx-agent agents deploy --target model-serving --name <catalog.schema.model>` from inside that project.

---

## Optional local project

Use `--no-yaml` when you want an editable project directory and local FastAPI dev UI before deploying.

```bash
uv run apx-agent agents scaffold my-agent --no-yaml
cd my-agent && uv sync
uv run apx-agent agents run --reload
```

This writes `my-agent/` with `agent.py`, `pyproject.toml`, `databricks.yml`, `agent_server/`, and scripts. FastAPI starts on `:8000` with file-watch reload; leave it running in one terminal and edit `agent.py` in your IDE in the other.

### Walk through the dev UI

The scaffolded agent is grounded against `samples.nyctaxi` by default. Walk through this before deploying:

1. **Open `http://localhost:8000`.** It redirects to `/_apx/agent` — a tabbed shell with **Chat · Edit · Eval · Probe** tabs and **Topology** / **Traces** header buttons.

2. **On the Chat tab**, ask "what tables can you query?" then "how many taxi trips are in the data?". The agent picks the SQL tool, runs the query as you (your OAuth token, your UC grants), and streams the answer with the tool call shown inline.

3. **Click Traces** to open `/_apx/traces`. Click a row to drill into the span tree — LLM call, tool call, SQL execution, response. Production traces look identical, stored in your workspace's MLflow experiment.

4. **Open `agent.py` in your IDE.** Change `instructions=` to something specific to your data, save. The server reloads; ask another question and watch the new behavior.

> **Connect your own data:** Click **Open Setup** in the chat panel (or go to `/_apx/setup`) — pick a catalog, schema, and SQL warehouse. Setup writes values to `.env` and can rewrite `agent.py` grounded in the real columns.

> **SQL query returns nothing?** The SQL warehouse is probably stopped. Open the **Probe** tab at `/_apx/probe/checks` to confirm and start it.

From inside `my-agent/`, deploy the project directory with:

```bash
uv run apx-agent agents deploy
```

This bundles the project and creates a Databricks App. `apx-agent agents deploy` prints the URL when it finishes.

---

## Confirm the deploy is live

1. **Check `/readyz`** — `curl <app-url>/readyz` returns `{"status":"ok"}` when the agent is running and all capabilities (memory, warehouse) are healthy.
2. **Ask a question** — open `<app-url>` in a browser and ask what you tested locally.
3. **Check traces** — go to **Machine Learning → Experiments** in your workspace and find your agent's experiment. A trace with a successful tool call confirms the full stack is working.

If the app shows a 502 or `/readyz` returns an error, run `uv run apx-agent doctor` and see [../deploy/troubleshooting.md](../deploy/troubleshooting.md).

---

## Updating your agent

After a YAML-first deploy, edit `my-agent.yaml` locally and rerun `apx-agent agents deploy my-agent.yaml --target apps`.

If you deployed from a `--no-yaml` project directory, `apx-agent agents deploy` uploads your code to a path in your Databricks workspace. To update that project without redeploying from the CLI:

1. Go to your workspace → **Apps** → select your app
2. Click **Edit source** to open `agent.py` in the workspace editor
3. Make your changes and save
4. Click **Restart** — the app re-reads `agent.py` and starts fresh at the same URL

No new deployment, no URL change.

---

## Agent types

The scaffolded agent is a `DataAgent`. If you need something different:

| You want | Use |
|---|---|
| Full control over tools and the agent loop | `LlmAgent` / `Agent` |
| A governed agent over one UC schema | `DataAgent` |
| Two source systems joined on a shared key | `CoworkerAgent` |
| A pipeline of agents in sequence | `SequentialAgent` |
| Conversational routing to specialists | `HandoffAgent` |

### LlmAgent — direct construction

If you know the tools you want and don't need schema auto-discovery:

```python
from apx_agent import Agent, uc_function_tool, genie_tool

agent = Agent(
    name="support_agent",
    instructions="Help users understand their account.",
    tools=[
        uc_function_tool("main.tools.lookup_account"),
        genie_tool("abc123", description="Answer billing questions"),
    ],
    max_iterations=10,
)
```

Run it the same way: `uv run apx-agent agents run --reload`.

> **If you know ADK or OpenAI Agents SDK:** `Agent(name, instructions, tools)` maps directly — same shape, same idea. `run_once(agent, "prompt")` is the no-HTTP runner. See [migration.md](migration.md) for the full mapping.

### CoworkerAgent — join two source systems

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    join_key="employee ID",
    objective="surface mismatches between hours worked and paychecks issued",
)
```

Scaffold one with:

```bash
apx-agent agents scaffold my-coworker --template coworker --no-yaml
```

(Drop `--no-yaml` to get a `my-coworker.yaml` spec you deploy directly instead of a local project.)

See [../agents/coworker.md](../agents/coworker.md) for the full reference.

---

## Add tools

Every tool is a Python function decorated with `@tool`, or a built-in governed primitive:

| Tool | What it wraps |
|---|---|
| `uc_function_tool("catalog.schema.fn")` | A Unity Catalog SQL function |
| `genie_tool("space_id")` | A Genie space — natural-language SQL over any data |
| `vector_search_tool("index_name")` | A Databricks Vector Search index |
| `@tool def fn(...) -> str` | Any Python function |

```python
from apx_agent import Agent, uc_function_tool, tool

@tool
def lookup_account(account_id: str) -> str:
    """Look up an account by ID."""
    return f"Account {account_id}: active"

agent = Agent(
    instructions="Help users understand their accounts.",
    tools=[
        lookup_account,
        uc_function_tool("main.tools.get_balance"),
    ],
)
```

See [../tools/custom-tools.md](../tools/custom-tools.md) for `http_tool`, `openapi_tool`, and MCP tools.

---

## Troubleshooting

`apx-agent doctor` is the first stop when something isn't working:

```bash
uv run apx-agent doctor            # full check including a live workspace round-trip
uv run apx-agent doctor --offline  # skip the network check (CI / offline)
uv run apx-agent doctor --json     # machine-readable output
```

**Auth errors** (`Could not resolve Databricks authentication`) — re-run `databricks auth login --profile <name>` and confirm with `databricks current-user me --profile <name>`, then restart `apx-agent agents run`.

**Warehouse stopped** — open the Probe tab at `/_apx/probe/checks`. It shows warehouse status and a link to start it.

**`apx-agent agents deploy` fails with Apps error** — confirm Apps is enabled under **Workspace Settings → Apps**. If not available, use `--target model-serving` instead.

For deploy failures, see [../deploy/troubleshooting.md](../deploy/troubleshooting.md).

---

## What's next

| Goal | Doc |
|---|---|
| Agent types, constructor params, `run()` / `stream()` | [../agents/llm-agent.md](../agents/llm-agent.md) |
| Join two source systems | [../agents/coworker.md](../agents/coworker.md) |
| Build a multi-step pipeline | [../agents/composition.md](../agents/composition.md) |
| Add multi-turn memory | [../running/sessions-and-memory.md](../running/sessions-and-memory.md) |
| Write and publish UC function tools | [../tools/overview.md](../tools/overview.md) |
| Choose Apps vs Model Serving | [../deploy/apps-vs-model-serving.md](../deploy/apps-vs-model-serving.md) |
| Guardrails and callbacks | [../safety/callbacks.md](../safety/callbacks.md) |
| Full CLI reference | [cli.md](cli.md) |
| Coming from ADK or OpenAI Agents SDK | [migration.md](migration.md) |
