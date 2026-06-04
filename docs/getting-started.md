# Getting started

An **agent** is a declaration: `instructions` + `tools` + a model name. apx-agent compiles it to whichever Databricks runtime you target without changing the agent definition.

## Prerequisites

```bash
# 1. uv (Python 3.11+ required)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. The Databricks CLI v2 — Homebrew is the recommended install. Older
#    docs reference `pip install databricks-cli`; that's the legacy v0.17
#    Python CLI and won't work with `databricks auth login` below.
brew tap databricks/tap && brew install databricks
#   (or:  curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh)

# 3. Authenticate against your workspace. Pick a profile name you'll remember.
databricks auth login --host https://<workspace>.cloud.databricks.com --profile <name>

# 4. Verify auth resolves — this MUST succeed before `apx run`, which fails
#    fast with "Could not resolve Databricks authentication" otherwise.
databricks current-user me --profile <name>

# 5. If you have multiple profiles, tell apx which one to use (either export
#    it for the shell session, or put it in the scaffolded project's .env).
export DATABRICKS_CONFIG_PROFILE=<name>
```

You'll also need a Databricks workspace with access to a model serving endpoint (e.g. `databricks-claude-sonnet-4-6`).

Once you have the CLI installed (below), run `apx doctor` to confirm everything is wired up before you touch `apx run` or `apx deploy`:

```bash
apx doctor            # checks Python, uv, Databricks CLI, auth, project layout
apx doctor --offline  # skip the live workspace round-trip (CI / offline)
```

It prints a `Fix:` line for anything wrong. Auth errors caught here are much cleaner than the errors you'd see mid-`apx run`.

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
uv run apx scaffold my-agent          # defaults to a Databricks Apps project
cd my-agent && uv sync
uv run apx deploy                     # target auto-detected from the project
```

(For a Mosaic AI Model Serving endpoint instead, scaffold with `--target model-serving` and deploy with `--target model-serving --name <catalog.schema.model>`.)

When it finishes, `apx deploy` prints the app URL. Open it — the scaffolded agent (a `DataAgent` over the built-in `samples.nyctaxi` dataset) is live and ready to chat.

---

## Your first 5 minutes

### What scaffold gave you

`apx scaffold my-agent` wrote this tree:

```
my-agent/
├── agent.py                  ← the one file you edit
├── pyproject.toml            ← deps + [tool.apx.agent] config
├── databricks.yml            ← Databricks bundle (used by `apx deploy`)
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
- **The browser at `http://localhost:8000`** — chat with the agent, see traces, run the setup wizard, edit tools through a UI. Every view lives under `/_apx/*` and is just a window onto the same `agent.py`.

The deployed app on Databricks Apps is the third surface — same code, same `/_apx/*` UI, just hosted. You don't need to deploy to develop.

### Run it

```bash
uv run apx run --reload
```

This starts FastAPI on `:8000` with file-watch reload. Leave it running in one terminal; edit `agent.py` in your IDE in the other.

### What you should see (walk this end-to-end)

1. **Open `http://localhost:8000`.** It redirects to `/_apx/agent` — a tabbed shell with four tabs (**Chat · Edit · Eval · Probe**) and two header buttons (**⧉ Topology** opens an in-page modal, **⏱ Traces** opens `/_apx/traces` in a new browser tab). The default tab is Chat.
2. **Spot the orange banner at the top of the chat panel:** *"👋 First time here? **Open Setup** to connect your data and generate tools automatically."* It appears whenever `DEMO_CATALOG` / `WAREHOUSE_ID` aren't set in `.env`. Click **Open Setup** — it takes you to the standalone `/_apx/setup` page.
3. **In `/_apx/setup`,** pick a catalog (try `samples`), a schema (try `nyctaxi`), and a SQL warehouse. Click **Save** — this writes the values to your project's `.env` and reloads the agent. Optionally tick "generate instructions" and Setup will inspect the schema and rewrite `agent.py`'s `instructions=` for you. Navigate back to `/_apx/agent` when done.
4. **Back on the Chat tab,** ask **"what tables can you query?"** then **"how many taxi trips are in the data?"**. The agent picks the SQL tool, runs the query as you (your OAuth token, your UC grants), and streams the answer with the tool call shown inline.
5. **Click the ⏱ Traces button in the header.** A new tab opens at `/_apx/traces` listing recent turns. Click a row to drill into the span tree — LLM call, tool call, SQL execution, response. This is what you'll deploy with; production traces look identical, just stored in the workspace's MLflow experiment.
6. **Open `agent.py` in your IDE.** Change `instructions=` to something specific to your data, save. The reload kicks in; ask another question and watch the new behavior. This is the loop.

If a SQL query returns nothing, the warehouse is probably stopped — open the **Probe** tab (or hit `/_apx/probe/checks`) to confirm and click through to start it. Auth errors at this point usually mean a stale token — re-run `databricks auth login --profile <name>` and restart `apx run`.

When it looks good, `uv run apx deploy` ships it.

See [docs/dev-ui.md](dev-ui.md) for the full `/_apx/*` surface map (topology graph, eval harness, outbound connectivity probe, deploy streamer).

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

### Want an agent that remembers?

`CoworkerAgent` is a `DataAgent` that also remembers across sessions. Same
one-liner, two extra knobs:

```python
from apx_agent import CoworkerAgent

agent = CoworkerAgent(
    "main", "payroll",
    persona="a payroll operations analyst",
    memory="persistent",   # UC Delta — no extra infra
)
```

Scaffold one with `apx scaffold my-coworker --template coworker`. See
[`docs/coworker.md`](coworker.md) for the full reference.

## Updating your agent

After the initial deploy, `apx deploy` uploads your code to a path in your Databricks workspace (visible in the deploy output as `Downloading source code from /Workspace/Users/.../src/...`). That's where your agent lives at runtime.

To update it without redeploying from the CLI:

1. Go to your workspace → **Apps** → select your app
2. Click **Edit source** to open `agent.py` in the workspace editor
3. Make your changes and save
4. Click **Restart** — the app stops the running process, re-reads `agent.py` from the workspace, and starts fresh at the same endpoint

No new deployment, no URL change.

---

## After deploy — confirming it works

`apx deploy` prints the app URL when it finishes. To confirm the agent is
live and answering:

1. **Open the URL** — it redirects to `/_apx/agent`, same chat UI as local dev.
2. **Ask a question** — same first questions you used locally: *"what tables
   can you query?"* then something data-specific. If the agent answers, the
   deploy is healthy.
3. **Check traces** — `/_apx/traces` on the deployed URL shows production
   spans stored in your workspace's MLflow experiment. A trace with a
   successful tool call confirms the full stack (auth, SQL warehouse, UC) is
   working end-to-end.

If the app shows a 502 or the agent can't reach the warehouse, run
`apx doctor` locally and check [`docs/deployment-troubleshooting.md`](deployment-troubleshooting.md).

---

## What's next

| Goal | Doc |
|------|-----|
| Agent that remembers across sessions | [coworker.md](coworker.md) |
| Choose Apps vs Model Serving | [apps-vs-model-serving.md](apps-vs-model-serving.md) |
| Build a multi-step pipeline | [workflow-patterns.md](workflow-patterns.md) |
| Add multi-turn memory | [sessions-and-memory.md](sessions-and-memory.md) |
| Write and publish UC function tools | [governed-primitives.md](governed-primitives.md) |
| DataAgent reference | [data-agent.md](data-agent.md) |
| pyproject.toml reference | [pyproject-toml.md](pyproject-toml.md) |
| Add compliance guards | [compliance.md](compliance.md) |
| Full CLI reference | [cli.md](cli.md) |

---

## Troubleshooting

`apx doctor` is the first thing to run when something isn't working:

```bash
apx doctor            # full check incl. a live workspace round-trip
apx doctor --offline  # skip the network check (CI / offline)
apx doctor --json     # machine-readable output
```

It reports your Python version, `uv`, the Databricks CLI, authentication, and
your project layout, with a `Fix:` line for anything wrong. The exit code is
non-zero if any check fails, so it is safe to use in CI preflights.

**Auth errors** (`Could not resolve Databricks authentication`) — re-run
`databricks auth login --profile <name>` and confirm with
`databricks current-user me --profile <name>`, then restart `apx run`.

**Warehouse stopped** — open the **Probe** tab at `/_apx/probe/checks`; it
shows warehouse status and a link to start it.

For deploy failures, see [`docs/deployment-troubleshooting.md`](deployment-troubleshooting.md).
