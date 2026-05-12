# Databricks Builder App

> **Security Notice:** This application wraps Claude Code. Projects created within the app by different users are not strongly isolated from each other (this project doesn't implement solutions like Firecracker microVM or Docker to isolate Claude sessions from the app). Only grant access to users you trust.

A reference implementation of a production Databricks app built on the [apx-agent SDK](https://github.com/stuagano/apx-agent). It demonstrates how to embed the SDK in a multi-user FastAPI backend with real-time streaming, session resumption, per-request credential injection, and MLflow tracing.

Users interact through a React chat UI. Each message spawns a Claude agent session via `ClaudeSDKClient`, which connects to a running `databricks-mcp-server` process (over SSE) for Databricks tool execution.

Optionally, the app can also serve as an **MCP server** for [Genie Code](https://docs.databricks.com/en/genie/genie-code.html) and other MCP clients, exposing all 71+ Databricks tools via the MCP protocol at `/mcp`.

## SDK Usage

This app is built on `claude_agent_sdk` from [apx-agent](https://github.com/stuagano/apx-agent). The core pattern in `server/services/agent.py`:

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import McpSSEServerConfig

# Connect to databricks-mcp-server running as a separate process
mcp_config = McpSSEServerConfig(type="sse", url="http://localhost:8080/sse")

options = ClaudeAgentOptions(
    cwd=str(project_dir),
    allowed_tools=allowed_tools,          # built-in + mcp__databricks__* tools
    permission_mode='bypassPermissions',
    resume=session_id,                     # resume previous conversation
    mcp_servers={'databricks': mcp_config},
    system_prompt=system_prompt,
    env=claude_env,                        # auth env vars for Claude subprocess
    include_partial_messages=True,         # token-by-token streaming
)

async with ClaudeSDKClient(options=options) as client:
    await client.query(message)
    async for msg in client.receive_response():
        yield msg   # stream to React frontend via SSE
```

Key SDK features demonstrated:
- **`ClaudeSDKClient`** — streaming client with MLflow `autolog()` tracing support
- **`McpSSEServerConfig`** — connect Claude to an external MCP server process over SSE
- **`include_partial_messages=True`** — token-by-token streaming to the frontend
- **`resume=session_id`** — conversation continuity across HTTP requests
- **`can_use_tool` / `HookMatcher`** — per-tool permission callbacks

## Architecture Overview

```
Browser (React)
     │  HTTP + SSE
     ▼
FastAPI backend ──► Lakebase (PostgreSQL) or SQLite (local dev)
     │
     │  claude_agent_sdk.ClaudeSDKClient
     │  (runs in fresh thread — see note below)
     ▼
Claude subprocess
     ├── SSE ──► databricks-mcp-server (:8080) ──► Databricks Workspace
     │              71 Databricks tools
     └── in-process ──► apx tools (workspace upload, app deploy/status)
```

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Web Application                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│  React Frontend (client/)           FastAPI Backend (server/)               │
│  ┌─────────────────────┐            ┌─────────────────────────────────┐     │
│  │ Chat UI             │◄──────────►│ /api/invoke_agent               │     │
│  │ Project Selector    │   SSE      │ /api/projects                   │     │
│  │ Conversation List   │            │ /api/conversations              │     │
│  └─────────────────────┘            └────────────────┬────────────────┘     │
└───────────────────────────────────────────────────────┼─────────────────────┘
                                                        │
                              ┌─────────────────────────▼──────────────────┐
                              │   apx-agent SDK (claude_agent_sdk)         │
                              │   ClaudeSDKClient + ClaudeAgentOptions     │
                              │   (fresh thread per request)               │
                              └──────────────────┬─────────────────────────┘
                                   mcp_servers={'databricks': sse_config}
                          ┌────────────────────────────────────────────────┐
                          │                          │                     │
          ┌───────────────▼──────────────┐  ┌───────▼────────────┐        │
          │ databricks-mcp-server        │  │ apx tools          │        │
          │ (separate process, SSE)      │  │ (in-process SDK)   │        │
          │ http://localhost:8080/sse    │  └────────────────────┘        │
          └───────────────┬──────────────┘                                │
                          │                                               │
                          ▼                                               │
          ┌────────────────────────────────────────┐                      │
          │           Databricks Workspace          │                     │
          │  SQL Warehouses  │  Unity Catalog       │                     │
          │  Clusters        │  Workspace Files     │                     │
          └────────────────────────────────────────┘
```

> **Note on fresh thread:** The SDK client runs in a separate thread with a fresh event loop and `copy_context()` to preserve Databricks auth contextvars. This is a workaround for [issue #462](https://github.com/anthropics/claude-agent-sdk-python/issues/462) in FastAPI/uvicorn contexts.

## How It Works

### 1. Agent Sessions

When a user sends a message, the backend creates a Claude agent session using `ClaudeSDKClient`:

```python
async with ClaudeSDKClient(options=options) as client:
    await client.query(message)
    async for msg in client.receive_response():
        # msg is one of: AssistantMessage, UserMessage,
        #                ResultMessage, SystemMessage, StreamEvent
        yield msg
```

Key features:
- **Session Resumption**: Each conversation stores a `session_id` (from `ResultMessage.session_id`) for context continuity
- **Streaming**: All events (text, thinking, tool_use, tool_result) stream to the frontend in real-time
- **Project Isolation**: Each project has its own working directory with sandboxed file access
- **MLflow Tracing**: `mlflow.anthropic.autolog()` is called before creating the client — traces include prompts, tool calls, and costs

### 2. Authentication Flow

The app supports multi-user authentication using per-request credentials:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Authentication Flow                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  Production (Databricks Apps)         Development (Local)                   │
│  ┌──────────────────────────┐         ┌──────────────────────────┐          │
│  │ Request Headers:         │         │ Environment Variables:   │          │
│  │ X-Forwarded-User         │         │ DATABRICKS_HOST          │          │
│  │ X-Forwarded-Access-Token │         │ DATABRICKS_TOKEN         │          │
│  └────────────┬─────────────┘         └────────────┬─────────────┘          │
│               │                                    │                        │
│               └──────────────┬─────────────────────┘                        │
│                              ▼                                              │
│               ┌──────────────────────────┐                                  │
│               │ set_databricks_auth()    │  (contextvars)                   │
│               │ - host                   │                                  │
│               │ - token                  │                                  │
│               └────────────┬─────────────┘                                  │
│                            ▼                                                │
│               ┌──────────────────────────┐                                  │
│               │ get_workspace_client()   │  (used by all tools)             │
│               │ - Returns client with    │                                  │
│               │   context credentials   │                                  │
│               └──────────────────────────┘                                  │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

**How it works:**

1. **Request arrives** — The FastAPI backend extracts credentials:
   - **Production**: `X-Forwarded-User` and `X-Forwarded-Access-Token` headers (set by Databricks Apps proxy)
   - **Development**: Falls back to `DATABRICKS_HOST` and `DATABRICKS_TOKEN` env vars

2. **Auth context set** — Before invoking the agent:
   ```python
   from databricks_tools_core.auth import set_databricks_auth, clear_databricks_auth

   set_databricks_auth(workspace_url, user_token)
   try:
       async for event in stream_agent_response(...):
           yield event
   finally:
       clear_databricks_auth()
   ```

3. **Tools use context** — All Databricks tools call `get_workspace_client()` which:
   - First checks contextvars for per-request credentials
   - Falls back to environment variables if no context set

This ensures each user's requests use their own Databricks credentials, enabling proper access control and audit logging.

### 3. MCP Integration (Databricks Tools)

Databricks tools are served by a **separate** `databricks-mcp-server` process. The builder app connects to it over SSE:

```python
from claude_agent_sdk.types import McpSSEServerConfig

# databricks-mcp-server must be running before the builder app starts
config = McpSSEServerConfig(
    type="sse",
    url=os.environ["DATABRICKS_MCP_SERVER_URL"],  # e.g. http://localhost:8080/sse
)

options = ClaudeAgentOptions(
    mcp_servers={'databricks': config},
    allowed_tools=['mcp__databricks__execute_sql', ...],
)
```

Tools are exposed as `mcp__databricks__<tool_name>` and include:
- SQL execution (`execute_sql`, `execute_sql_multi`)
- Warehouse and cluster management (`list_warehouses`, `get_best_cluster`)
- Pipeline management (`create_or_update_pipeline`, `start_update`, etc.)
- File operations (`upload_to_volume`, `download_from_volume`)
- Genie (`create_or_update_genie`, `ask_genie`)
- Unity Catalog (`manage_uc_objects`, `manage_uc_grants`, etc.)

The MCP server process is started by `scripts/start_local.sh` and runs alongside the backend.

### 4. Skills System

Skills provide specialized guidance for Databricks development tasks. They are markdown files with instructions and examples that Claude can load on demand.

**Skill loading flow:**
1. On startup, skills are copied from `../databricks-skills/` to `./skills/`
2. When a project is created, skills are copied to `project/.claude/skills/`
3. The agent can invoke skills using the `Skill` tool: `skill: "sdp"`

Skills include:
- **databricks-bundles**: DABs configuration
- **databricks-app-apx**: Full-stack apps with APX framework (FastAPI + React)
- **databricks-app-python**: Python apps with Dash, Streamlit, Flask
- **databricks-python-sdk**: Python SDK patterns
- **databricks-mlflow-evaluation**: MLflow evaluation and trace analysis
- **databricks-spark-declarative-pipelines**: Spark Declarative Pipelines (SDP) development
- **databricks-synthetic-data-gen**: Creating test datasets

### 5. Project Persistence

Projects are stored in the local filesystem with automatic backup to PostgreSQL:

```
projects/
  <project-uuid>/
    .claude/
      skills/        # Copied skills for this project
    src/             # User's code files
    ...
```

**Backup system:**
- After each agent interaction, the project is marked for backup
- A background worker runs every 10 minutes
- Projects are zipped and stored in PostgreSQL (Lakebase)
- On access, missing projects are restored from backup

## Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- [uv](https://github.com/astral-sh/uv) package manager
- Databricks workspace with:
  - SQL warehouse (for SQL queries)
  - Cluster (for Python/PySpark execution)
  - Unity Catalog enabled (recommended)
- PostgreSQL database (Lakebase) for project persistence in production — or SQLite for local dev

### Quick Start (Local Development)

One command installs all dependencies and starts the full stack (MCP server + backend + frontend):

```bash
cd databricks-builder-app

# With Lakebase (full production-equivalent setup)
./scripts/start_local.sh --profile <your-profile>

# With SQLite (no Lakebase required — fastest way to start)
DATABASE_URL=sqlite+aiosqlite:///./builder.db \
  ./scripts/start_local.sh --profile <your-profile> --skip-lakebase
```

This will:
- Check prerequisites (uv, Node.js, npm, Databricks CLI v0.287.0+)
- Get credentials from your Databricks CLI profile
- Provision a Lakebase Autoscale database via DAB (unless `--skip-lakebase`)
- Generate `.env.local` with your workspace settings
- Install backend and frontend dependencies
- Install Databricks skills (local + external)
- Run database migrations (`alembic upgrade head`)
- Start `databricks-mcp-server` (SSE on :8080)
- Start backend (http://localhost:8000)
- Start frontend (http://localhost:3000)

#### Options

```bash
# First time — everything from scratch
./scripts/start_local.sh --profile dbx_shared_demo

# Subsequent runs — fast (deps cached, Lakebase exists)
./scripts/start_local.sh --profile dbx_shared_demo

# Skip Lakebase provisioning (use --skip-lakebase with DATABASE_URL=sqlite://...)
./scripts/start_local.sh --profile dbx_shared_demo --skip-lakebase

# Force reinstall all dependencies
./scripts/start_local.sh --profile dbx_shared_demo --force-install

# Regenerate .env.local
./scripts/start_local.sh --profile dbx_shared_demo --force-env

# Custom Lakebase project name
./scripts/start_local.sh --profile dbx_shared_demo --lakebase-id my-custom-db
```

#### Access the App

- **Frontend**: <http://localhost:3000>
- **Backend API**: <http://localhost:8000>
- **API Docs**: <http://localhost:8000/docs>
- **MCP Server** (SSE): <http://localhost:8080/sse>

Press `Ctrl+C` to stop all servers.

#### (Optional) Configure Claude via Databricks Model Serving

If you're routing Claude API calls through Databricks Model Serving instead of directly to Anthropic, create `.claude/settings.json` in the **repository root** (not in the app directory):

```json
{
    "env": {
        "ANTHROPIC_MODEL": "databricks-claude-sonnet-4-5",
        "ANTHROPIC_BASE_URL": "https://your-workspace.cloud.databricks.com/serving-endpoints/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "dapi...",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-5",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-4-5"
    }
}
```

Notes:

- `ANTHROPIC_AUTH_TOKEN` should be a Databricks PAT, not an Anthropic API key
- `ANTHROPIC_BASE_URL` should point to your Databricks Model Serving endpoint
- If this file doesn't exist, the app uses your `ANTHROPIC_API_KEY` from `.env.local`

### Configuration Details

#### Databricks Authentication Modes

The app supports two authentication modes:

**1. Local Development (Environment Variables)**
- Uses `DATABRICKS_HOST` and `DATABRICKS_TOKEN` from `.env.local`
- All users share the same credentials
- Good for local development and testing

**2. Production (Request Headers)**
- Uses `X-Forwarded-User` and `X-Forwarded-Access-Token` headers
- Set automatically by Databricks Apps proxy
- Each user has their own credentials
- Proper multi-user isolation

#### Database Configuration

The app supports two database backends:

| Mode | Config | Use when |
|------|--------|----------|
| **SQLite** | `DATABASE_URL=sqlite+aiosqlite:///./builder.db` | Local dev without Lakebase |
| **PostgreSQL (Lakebase)** | `LAKEBASE_ENDPOINT=...` or `LAKEBASE_INSTANCE_NAME=...` | Production or full local setup |

SQLite stores the database at `./builder.db` in the app directory. Migrations run automatically on startup for both backends.

#### Skills Configuration

Skills are loaded from `../databricks-skills/` and filtered by the `ENABLED_SKILLS` environment variable:

- `databricks-python-sdk`: Patterns for using the Databricks Python SDK
- `databricks-spark-declarative-pipelines`: SDP/DLT pipeline development
- `databricks-synthetic-data-gen`: Creating test datasets
- `databricks-app-apx`: Full-stack apps with React (APX framework)
- `databricks-app-python`: Python apps with Dash, Streamlit, Flask

**Adding custom skills:**
1. Create a new directory in `../databricks-skills/`
2. Add a `SKILL.md` file with frontmatter:
   ```markdown
   ---
   name: my-skill
   description: "Description of the skill"
   ---

   # Skill content here
   ```
3. Add the skill name to `ENABLED_SKILLS` in `.env.local`

#### Database Setup

The app uses PostgreSQL (Lakebase) or SQLite for:
- Project metadata
- Conversation history
- Message storage
- Project backups (zipped project files)

**Migrations:**
```bash
# Run migrations (done automatically on startup)
DATABASE_URL=sqlite+aiosqlite:///./builder.db alembic upgrade head

# Create a new migration
alembic revision --autogenerate -m "description"
```

### Troubleshooting

#### MCP server not connecting

The `databricks-mcp-server` must be running before the builder app starts. Check:
```bash
# Is the server running?
lsof -i:8080

# Start it manually
cd ../databricks-mcp-server
DATABRICKS_HOST=... DATABRICKS_TOKEN=... \
  ../.venv/bin/python run_server.py --transport sse --port 8080
```

The builder app reads `DATABRICKS_MCP_SERVER_URL` (e.g. `http://localhost:8080/sse`) to locate the server. `start_local.sh` sets this automatically.

#### Skills not loading

Check:
1. `ENABLED_SKILLS` environment variable in `.env.local`
2. Skill names match directory names in `../databricks-skills/`
3. Each skill has a `SKILL.md` file with proper frontmatter
4. Check logs: `Copied X skills to ./skills`

#### Databricks authentication failing

Check:
1. `DATABRICKS_HOST` is correct (no trailing slash)
2. `DATABRICKS_TOKEN` is valid and not expired
3. Token has proper permissions (cluster access, SQL warehouse access, etc.)
4. If using Databricks Model Serving, check `.claude/settings.json` configuration

#### Port already in use

```bash
# Kill processes on ports 8000, 8080, and 3000
lsof -ti:8000 | xargs kill -9
lsof -ti:8080 | xargs kill -9
lsof -ti:3000 | xargs kill -9
```

### Production Build

```bash
# Build frontend
cd client && npm run build && cd ..

# Run with uvicorn (MCP server must be started separately)
uvicorn server.app:app --host 0.0.0.0 --port 8000
```

## Project Structure

```
databricks-builder-app/
├── server/                 # FastAPI backend
│   ├── app.py             # Main FastAPI app
│   ├── db/                # Database models and migrations
│   │   ├── models.py      # SQLAlchemy models
│   │   └── database.py    # Session management (PostgreSQL + SQLite)
│   ├── routers/           # API endpoints
│   │   ├── agent.py       # /api/agent/* (invoke, etc.)
│   │   ├── projects.py    # /api/projects/*
│   │   └── conversations.py
│   ├── mcp_gateway.py     # MCP Gateway for Genie Code (optional, via ENABLE_MCP_GATEWAY)
│   └── services/          # Business logic
│       ├── agent.py       # ClaudeSDKClient + ClaudeAgentOptions (SDK core)
│       ├── databricks_tools.py  # McpSSEServerConfig + TOOL_NAMES
│       ├── user.py        # User auth (headers/env vars)
│       ├── skills_manager.py
│       ├── backup_manager.py
│       └── system_prompt.py
├── client/                # React frontend
│   ├── src/
│   │   ├── pages/         # Main pages (ProjectPage, etc.)
│   │   └── components/    # UI components
│   └── package.json
├── alembic/               # Database migrations
├── scripts/               # Utility scripts
│   ├── start_local.sh     # Local development (one command)
│   └── _legacy/           # Old setup.sh and start_dev.sh
├── skills/                # Cached skills (gitignored)
├── projects/              # Project working directories (gitignored)
├── pyproject.toml         # Python dependencies
└── .env.example           # Environment template
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/me` | GET | Get current user info |
| `/api/health` | GET | Health check |
| `/api/system_prompt` | GET | Preview the system prompt |
| `/api/projects` | GET | List all projects |
| `/api/projects` | POST | Create new project |
| `/api/projects/{id}` | GET | Get project details |
| `/api/projects/{id}` | PATCH | Update project name |
| `/api/projects/{id}` | DELETE | Delete project |
| `/api/projects/{id}/conversations` | GET | List project conversations |
| `/api/projects/{id}/conversations` | POST | Create new conversation |
| `/api/projects/{id}/conversations/{cid}` | GET | Get conversation with messages |
| `/api/projects/{id}/files` | GET | List files in project directory |
| `/api/invoke_agent` | POST | Start agent execution (returns execution_id) |
| `/api/stream_progress/{execution_id}` | POST | SSE stream of agent events |
| `/api/stop_stream/{execution_id}` | POST | Cancel an active execution |
| `/api/projects/{id}/skills/available` | GET | List skills with enabled status |
| `/api/projects/{id}/skills/enabled` | PUT | Update enabled skills for project |
| `/api/projects/{id}/skills/reload` | POST | Reload skills from source |
| `/api/projects/{id}/skills/tree` | GET | Get skills file tree |
| `/api/projects/{id}/skills/file` | GET | Get skill file content |
| `/api/clusters` | GET | List available Databricks clusters |
| `/api/warehouses` | GET | List available SQL warehouses |
| `/api/mlflow/status` | GET | Get MLflow tracing status |
| **MCP Gateway** (when `ENABLE_MCP_GATEWAY=true`) | | |
| `/mcp` | POST | MCP protocol endpoint (Streamable HTTP) |
| `/mcp/health` | GET | MCP gateway health check |
| `/mcp/tools` | GET | List all registered MCP tools |
| `/mcp/skills` | GET | List all available skills |
| `/mcp/info` | GET | HTML info page with tools and skills |

## Deploying to Databricks Apps

The Builder App uses an automated deploy script that provisions all infrastructure and deploys the app in a single command.

### Prerequisites

- **Databricks CLI v0.287.0+** — [Install](https://docs.databricks.com/aws/en/dev-tools/cli/install)
- **Node.js 18+** — for building the frontend
- **uv** — Python package manager ([Install](https://github.com/astral-sh/uv))
- **Databricks workspace** with Lakebase Autoscaling enabled

### Quick Deploy

```bash
cd databricks-builder-app

# Full deploy — creates Lakebase, builds frontend, installs skills, creates app, grants permissions, deploys
./scripts/deploy.sh <app-name> --profile <your-profile>
```

That's it. The script handles everything:

| Step | What the script does |
|------|---------------------|
| 1 | Checks prerequisites (CLI version, auth) |
| 2 | Provisions Lakebase Autoscale via Databricks Asset Bundle (`databricks.yml`) |
| 3 | Builds the React frontend |
| 4 | Stages server code, packages, skills, and generates `app.yaml` |
| 5 | Creates the Databricks App (if it doesn't exist) |
| 6 | Creates Lakebase OAuth role and grants PostgreSQL permissions for the app's service principal |
| 7 | Uploads everything to workspace |
| 8 | Deploys the app |

### Deploy Options

```bash
# Full deploy from scratch
./scripts/deploy.sh my-builder-app --profile dbx_shared_demo

# Deploy with MCP Gateway for Genie Code (name MUST start with mcp-)
./scripts/deploy.sh mcp-builder-app --enable-mcp --profile dbx_shared_demo

# Quick redeploy (skip Lakebase + frontend build + skills download)
./scripts/deploy.sh my-builder-app --profile dbx_shared_demo --skip-lakebase --skip-build --skip-skills

# Custom Lakebase project name
./scripts/deploy.sh my-builder-app --profile dbx_shared_demo --lakebase-id my-custom-db

# All options
./scripts/deploy.sh --help
```

### What Gets Created

| Resource | Details |
|----------|---------|
| **Lakebase Autoscale project** | PostgreSQL 17, 0.5-2 CU, scale-to-zero after 5 min |
| **Databricks App** | FastAPI backend + React frontend |
| **Lakebase OAuth role** | For the app's service principal |
| **PostgreSQL schema** | `builder_app` with full grants for the SP |
| **Database tables** | Created automatically via alembic migrations on first startup |

### Infrastructure as Code

The Lakebase database is managed declaratively via a Databricks Asset Bundle (`databricks.yml`):

```yaml
bundle:
  name: databricks-builder-app

variables:
  lakebase_project_id:
    description: "Lakebase project ID"
    default: "builder-app-db"

resources:
  postgres_projects:
    builder_db:
      project_id: ${var.lakebase_project_id}
      display_name: "builder-app-db"
      pg_version: 17
      default_endpoint_settings:
        autoscaling_limit_min_cu: 0.5
        autoscaling_limit_max_cu: 2
        suspend_timeout_duration: "300s"
```

You can manage the Lakebase infrastructure independently:

```bash
# Deploy/update Lakebase only
databricks bundle deploy --profile <profile>

# Destroy Lakebase (does NOT affect the app)
databricks bundle destroy --profile <profile>
```

### Redeploying After Code Changes

```bash
# Full redeploy (rebuilds everything)
./scripts/deploy.sh my-builder-app --profile <profile>

# Quick redeploy (server code changes only)
./scripts/deploy.sh my-builder-app --profile <profile> --skip-lakebase --skip-build --skip-skills
```

### MCP Gateway for Genie Code

The Builder App can optionally serve as an **MCP server** at `/mcp`, exposing all 71+ Databricks tools to [Genie Code](https://docs.databricks.com/en/genie/genie-code.html), AI Playground, and other MCP clients. This turns the app into a dual-purpose deployment: **visual builder UI** at `/` and **MCP server** at `/mcp`.

#### How It Works

```
┌─────────────────────────────────────────────────────┐
│  Builder App (single Databricks App deployment)     │
│                                                     │
│  /              → React Builder UI                  │
│  /api/*         → REST API (projects, agent, etc.)  │
│                                                     │
│  /mcp           → MCP Protocol (Streamable HTTP)    │
│  /mcp/health    → Health check (JSON)               │
│  /mcp/tools     → Tool listing (JSON)               │
│  /mcp/skills    → Skill listing (JSON)              │
│  /mcp/info      → Info page (HTML)                  │
└─────────────────────────────────────────────────────┘
```

#### Deploying with MCP Gateway

> **Genie Code requires app names to start with `mcp-`** to appear in the MCP server picker. The deploy script will warn you if the name doesn't match.

```bash
# Deploy with MCP Gateway enabled (Genie Code compatible name)
./scripts/deploy.sh mcp-builder-app --enable-mcp --profile <your-profile>

# Quick redeploy (code changes only)
./scripts/deploy.sh mcp-builder-app --enable-mcp --skip-lakebase --skip-build --skip-skills --profile <profile>
```

The `--enable-mcp` flag sets `ENABLE_MCP_GATEWAY=true` and `FASTMCP_STATELESS_HTTP=true` in the generated `app.yaml`. Without this flag, the MCP gateway is completely disabled and the app behaves identically to a standard deployment.

#### Registering with Genie Code

After deploying with `--enable-mcp` and an `mcp-` prefixed name:

1. Open a **Genie Space** in the Databricks UI
2. Click the **gear icon** (Settings) > **MCP Servers**
3. Select your app (e.g., `mcp-builder-app`) from the list
4. Genie Code now has access to all Databricks tools via MCP

You can also install skills to the Genie Space for additional context:

```bash
# From the repo root — installs skills to your workspace for Genie Code
./databricks-skills/install_skills_to_genie_code.sh
```

#### Using with Other MCP Clients

The MCP endpoint works with any MCP client that supports Streamable HTTP transport:

```
MCP URL: https://<app-url>/mcp
```

| Client | Configuration |
|--------|---------------|
| **Genie Code** | Settings > MCP Servers > Select app |
| **AI Playground** | Add MCP server URL |
| **Claude Desktop** | `mcpServers` config with HTTP transport |
| **Cursor / VS Code** | MCP server config with HTTP transport |

### Destroying Everything

```bash
# Delete the app
databricks apps delete my-builder-app --profile <profile>

# Delete the Lakebase database
databricks bundle destroy --profile <profile> --auto-approve
```

### MLflow Tracing

The app automatically traces Claude Code conversations to MLflow. Traces include user prompts, Claude responses, tool usage, and session metadata.

The deploy script configures tracing to the `/Workspace/Shared/builder_app_ml_trace` experiment by default. To customize, edit the `MLFLOW_EXPERIMENT_NAME` value in the generated `app.yaml` section of `scripts/deploy.sh`.

See the [Databricks MLflow Tracing documentation](https://docs.databricks.com/aws/en/mlflow3/genai/tracing/integrations/claude-code) for more details.

### Deployment Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| CLI version too old | Need v0.287.0+ for Lakebase DAB support | `curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh \| sh` |
| `project with such id already exists` | Lakebase project name conflict | Use `--lakebase-id <different-name>` or destroy existing: `databricks bundle destroy` |
| `password authentication failed` | Lakebase OAuth role not created | Re-run deploy — Step 6 handles this automatically |
| `permission denied for table` | PostgreSQL grants missing | Re-run deploy — Step 6 is idempotent |
| `relation does not exist` | Migrations didn't run | Redeploy the app to trigger migrations |
| App shows blank page | Check logs: `databricks apps logs <app-name>` | Usually a package install error — check requirements.txt |

## Related

- **[databricks-mcp-server](../databricks-mcp-server/)** — The MCP server process this app connects to
- **[databricks-tools-core](../databricks-tools-core/)** — Core auth and tool primitives
- **[databricks-skills](../databricks-skills/)** — Skill definitions for Databricks development tasks
- **[apx-agent SDK](https://github.com/stuagano/apx-agent)** — The `claude_agent_sdk` package used here
