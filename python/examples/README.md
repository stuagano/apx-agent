# apx-agent Examples

Example applications built on the [apx-agent SDK](https://github.com/stuagano/apx-agent).

See **[EXAMPLES.md](./EXAMPLES.md)** for the full index with descriptions.

---

## Highlighted Example: Databricks Builder App

[`databricks-builder-app/`](./databricks-builder-app/) is a full-stack reference implementation showing how to build a production web app on the apx-agent SDK.

**What it demonstrates:**

- **`ClaudeSDKClient`** — streaming agent with session resumption and MLflow tracing
- **`McpSSEServerConfig`** — connecting Claude to an external MCP server process over SSE
- **Per-request credential injection** via `contextvars` (multi-user Databricks auth)
- **FastAPI + React** — real-time streaming of agent events to a chat UI
- **SQLite / Lakebase** — database backend with Alembic migrations

**Stack:**

```
React UI → FastAPI backend → ClaudeSDKClient → databricks-mcp-server (SSE)
                                                      ↓
                                             Databricks Workspace
```

**Quick start (local, SQLite, no Lakebase):**

```bash
cd databricks-builder-app
DATABASE_URL=sqlite+aiosqlite:///./builder.db \
  ./scripts/start_local.sh --profile <your-databricks-profile> --skip-lakebase
```

See [`databricks-builder-app/README.md`](./databricks-builder-app/README.md) for full setup and deploy instructions.

---

## Other Examples

See [EXAMPLES.md](./EXAMPLES.md) for the complete list.
