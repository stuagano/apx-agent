# Deploy the Databricks Builder App

One command deploys the full stack to your workspace: Lakebase database, frontend build, skills, and the app itself.

## No terminal? Deploy from your browser

Go to **[stuagano.github.io/apx-agent](https://stuagano.github.io/apx-agent)**, fill in your workspace URL and a Databricks access token, and click Deploy. No GitHub account, no terminal, nothing to install.

The page shows live progress and prints the app URL when it's done.

---

### One-time setup (Stuart only)

To make the deploy page work, create a fine-grained GitHub PAT and add it as a repo secret:

1. Go to **github.com → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. Set:
   - **Repository access:** Only `stuagano/apx-agent`
   - **Permissions:** `Actions` → Read and write
3. Copy the token
4. In this repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `GH_TRIGGER_TOKEN`
   - Value: the token you just copied
5. In this repo → **Settings → Pages**
   - Source: **GitHub Actions**
6. Push any change to `docs/deploy.html` (or run the **Publish Deploy Page** workflow manually) to publish the page

## Prerequisites

Install these once if you don't have them:

```bash
# 1. Databricks CLI v0.287.0+ (required for Lakebase DAB support)
curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh
databricks --version  # should be ≥ 0.287.0

# 2. uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Node.js 18+ (for frontend build)
# macOS: brew install node
# or: https://nodejs.org/en/download
node --version  # should be ≥ 18
```

You also need a Databricks CLI profile with admin access to your workspace:

```bash
databricks configure --profile my-workspace
# or OAuth: databricks auth login --profile my-workspace
```

## Deploy

```bash
# Clone the repo
git clone https://github.com/stuagano/apx-agent.git
cd apx-agent/python/examples/databricks-builder-app

# 1. Run preflight — checks your tools and workspace are ready
#    Replace "my-workspace" with your Databricks CLI profile name
./scripts/preflight.sh --profile my-workspace

# 2. If preflight passes, it prints the exact deploy command to run.
#    It will look like one of these:
./scripts/deploy.sh my-builder-app --profile my-workspace
./scripts/deploy.sh my-builder-app --profile my-workspace --skip-lakebase
```

That's it. The script handles everything in ~5 minutes:

| Step | What happens |
|------|-------------|
| 1 | Checks CLI version and auth |
| 2 | Provisions Lakebase Autoscale database |
| 3 | Builds the React frontend |
| 4 | Packages server code, dependencies, and skills |
| 5 | Creates the Databricks App |
| 6 | Grants the app's service principal PostgreSQL access |
| 7 | Uploads everything to your workspace |
| 8 | Deploys and starts the app |

The app URL is printed at the end. Open it in a browser — you should see the chat UI immediately.

## Requirements on your workspace

| Requirement | Why |
|-------------|-----|
| **Lakebase Autoscale enabled** | Project persistence (database backend) |
| **Claude via FMAPI** | The app routes Claude calls through your workspace's FM endpoint — no Anthropic key needed |
| **Databricks Apps enabled** | Obviously |

If your workspace doesn't have Lakebase, add `--skip-lakebase` to the deploy command. The app will use SQLite instead — projects won't survive app restarts, but everything else works fine for demos.

```bash
./scripts/deploy.sh my-builder-app --profile my-workspace --skip-lakebase
```

## Redeploy after changes

```bash
# Quick redeploy (skip Lakebase + frontend rebuild)
./scripts/deploy.sh my-builder-app --profile my-workspace --skip-lakebase --skip-build --skip-skills
```

## Options

| Flag | What it does |
|------|-------------|
| `--skip-lakebase` | Skip Lakebase provisioning (uses SQLite) |
| `--skip-build` | Skip frontend build (reuse previous build) |
| `--skip-skills` | Skip skills reinstall (reuse cached skills) |
| `--enable-mcp` | Expose `/mcp` endpoint for Genie Code (app name must start with `mcp-`) |
| `--lakebase-id ID` | Custom Lakebase project name (default: `builder-app-db`) |
| `--profile PROFILE` | Databricks CLI profile |

## Troubleshoot

**`error: resource type 'postgres_projects' is not supported`**
→ Lakebase Autoscale isn't enabled on this workspace. Use `--skip-lakebase`.

**App starts but shows a blank page or 500 errors**
→ Check logs: `databricks apps logs my-builder-app --profile my-workspace`

**`DATABRICKS_MCP_SERVER_URL is not set`**
→ This shouldn't happen with a fresh deploy — the `start.sh` sets it. If you see this in logs, redeploy.

**CLI version error**
→ Run `curl -fsSL https://raw.githubusercontent.com/databricks/setup-cli/main/install.sh | sh` to update.
