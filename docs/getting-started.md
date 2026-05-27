# Getting started

An **agent** is a declaration: `instructions` + `tools` + a model name. apx-agent compiles it to whichever Databricks runtime you target without changing the agent definition.

## Prerequisites

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # install uv (Python 3.11+ required)
pip install databricks-cli
databricks configure          # enter workspace URL + personal access token
```

You'll also need a Databricks workspace with access to a model serving endpoint (e.g. `databricks-claude-sonnet-4-6`).

### Installing apx-agent

It's not on PyPI yet — install straight from GitHub. The package lives in the `python/` subdirectory, so the URL needs `#subdirectory=python`:

```bash
uv add "apx-agent[langgraph] @ git+https://github.com/stuagano/apx-agent.git@v0.2.2#subdirectory=python"
```

Pin a release tag (`@v0.2.2`) for stability or `@main` for the latest. Or download the wheel from the [Releases page](https://github.com/stuagano/apx-agent/releases). The clone-and-`uv sync` flow below is the easiest way to scaffold + deploy.

---

## Deploy to Databricks Apps

Five commands from clone to a live agent:

```bash
git clone https://github.com/stuagano/apx-agent.git
cd apx-agent/python && uv sync
uv run apx scaffold my-agent --target apps
cd my-agent && uv sync
uv run apx deploy --target apps
```

When it finishes, `apx deploy` prints the app URL. Open it — the scaffolded agent (a `DataAgent` over the built-in `samples.nyctaxi` dataset) is live and ready to chat.

---

## Test locally first

Before deploying, run the agent on your machine:

```bash
uv run uvicorn agent_server.start_server:app --host 127.0.0.1 --port 8000
```

Open `http://localhost:8000/_apx/agent` and try **"what tables can you query?"** or **"how many taxi trips are in the data?"**. When it looks good, run `apx deploy`.

---

## The agent file

`agent.py` is the only file you edit. The scaffolded version is a one-line **`DataAgent`** — a governed agent over a Unity Catalog schema:

```python
from apx_agent import DataAgent

# Governed data agent over the built-in samples.nyctaxi dataset.
agent = DataAgent("samples", "nyctaxi")
```

Point it at your own data by changing the catalog/schema, and pass `ws=WorkspaceClient()` to have it auto-discover the schema's tables and UC functions and ground its instructions in the real columns:

```python
from databricks.sdk import WorkspaceClient
agent = DataAgent("main", "sales", ws=WorkspaceClient())
```

`DataAgent` is just a specialized `Agent`, so you can also drop to the general form and wire tools yourself:

| Tool | What it wraps |
|------|---------------|
| `uc_function_tool("catalog.schema.fn")` | A Unity Catalog SQL function |
| `genie_tool("space_id")` | A Genie space — natural-language SQL over any data |
| `vector_search_tool("index_name")` | A Databricks Vector Search index |
| `@tool def fn(...) -> str` | Any Python function |

## Updating your agent

After the initial deploy, `apx deploy` uploads your code to a path in your Databricks workspace (visible in the deploy output as `Downloading source code from /Workspace/Users/.../src/...`). That's where your agent lives at runtime.

To update it without redeploying from the CLI:

1. Go to your workspace → **Apps** → select your app
2. Click **Edit source** to open `agent.py` in the workspace editor
3. Make your changes and save
4. Click **Restart** — the app stops the running process, re-reads `agent.py` from the workspace, and starts fresh at the same endpoint

No new deployment, no URL change.

---

## What's next

| Goal | Doc |
|------|-----|
| Choose Apps vs Model Serving | [apps-vs-model-serving.md](apps-vs-model-serving.md) |
| Build a multi-step pipeline | [workflow-patterns.md](workflow-patterns.md) |
| Add multi-turn memory | [sessions-and-memory.md](sessions-and-memory.md) |
| Write and publish UC function tools | [governed-primitives.md](governed-primitives.md) |
| Add compliance guards | [compliance.md](compliance.md) |
| Full CLI reference | [cli.md](cli.md) |
