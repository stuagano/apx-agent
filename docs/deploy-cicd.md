# Deploy via CI/CD (scaffolded Apps projects)

`apx scaffold --target apps` (default `--ci github`) emits pipelines that
mirror agent-foundry's three-stage branch flow. `dev` is laptop-only; CI
never deploys it.

## Flow

| Trigger | Jobs |
|---------|------|
| PR → `main` | unit tests (`pytest`) |
| PR → `release` | unit tests + `apx deploy --target apps --bundle-target staging --no-run` |
| Push to `release` | gated `apx deploy --target apps --bundle-target prod` |

```text
feature/* ──PR──► main ──PR──► release ──push──► prod deploy
                 unit        staging deploy      (approval gate)
```

## Generated files

**GitHub** (`--ci github`, default):

- `.github/workflows/pr-to-main.yml`
- `.github/workflows/pr-to-release.yml`
- `.github/workflows/release-deploy-prod.yml`

**GitLab** (`--ci gitlab`):

- `.gitlab-ci.yml`

**Skip:** `--ci none`

Bundle targets in `databricks.yml`: `dev` (default, local), `staging`
(CI), `prod` (CI + gated). Staging apps are named `<app>-staging` so they
do not collide with prod.

## Secrets

| Secret | Used by |
|--------|---------|
| `DATABRICKS_HOST_STAGING` | PR → release |
| `DATABRICKS_CLIENT_ID_STAGING` | PR → release |
| `DATABRICKS_CLIENT_SECRET_STAGING` | PR → release |
| `DATABRICKS_HOST_PROD` | push → release |
| `DATABRICKS_CLIENT_ID_PROD` | push → release |
| `DATABRICKS_CLIENT_SECRET_PROD` | push → release |
| `FRAMEWORK_REPO_TOKEN` | optional — only if `apx-agent` is pinned from a *private* git URL |

The Databricks SDK auto-detects `DATABRICKS_HOST` / `CLIENT_ID` /
`CLIENT_SECRET` from the job env (templates map the `_*_STAGING` /
`_*_PROD` secrets into those names).

### GitHub setup

1. Create branches `main` and `release`.
2. **Settings → Secrets and variables → Actions** — add the secrets above.
3. **Settings → Environments → New environment** named `prod`, with
   required reviewers. The `release-deploy-prod` workflow references
   `environment: prod`.

### GitLab setup

1. Same branch names.
2. **Settings → CI/CD → Variables** — add the secrets (masked).
3. Prod job uses `when: manual` on the `release` branch.

## Local mirrors

```bash
uv sync --group dev
uv run pytest -q
uv run apx deploy --target apps --bundle-target staging --no-run
uv run apx status --bundle-target staging
uv run apx destroy --bundle-target staging --yes
uv run apx deploy --target apps --bundle-target prod
```

## Workspace deploy state

Successful `apx deploy --target apps` writes a JSON record to the
workspace (best-effort; deploy still succeeds if the write fails):

```text
/Shared/apx-agent/<app_name>/_state/<bundle_target>.json
```

Fields include `app_url`, `experiment_id`, framework pin / SHA, wheel
name, `deployed_at`, and a capped `_audit` trail. Read it with
`apx status`; remove the App + clear the file with `apx destroy`.

## Native App release history and rollback

Independent of the Bundle UC ledger, the platform keeps native App deployment
history for any existing App:

```bash
uv run apx releases history --app-name my-app --profile prod
uv run apx releases rollback --app-name my-app --deployment-id <exact-id> --profile prod
```

`history` reports exact deployment IDs, the active deployment ID, state/mode, and
packaged-source snapshot paths. `rollback` redeploys the exact successful snapshot
you name into the **same App** and verifies it became active. It is an
explicit-authorization command with no interactive confirmation, so pass the exact
ID you intend. CLI errors are deliberately generic — inspect native deployment
status in the workspace rather than expecting command or environment values in
CLI output.

This restores an **exact packaged-source snapshot, not an immutable installed
runtime or dependency set**, and **cross-App promotion is unsupported** (packaged
source and configuration may embed stage identity or environment). Current App
governance applies; historical permissions are not restored.

## Release ledger

`apx deploy --target apps` records each release as a Unity Catalog model-version
manifest under the configured `registered_model`, tagged with the App name, role,
git SHA, Bundle target, dirty-tree flag, and lockfile SHA-256. These commands apply
to **Bundle (`responses_agent`) projects only**; native Agent Bricks deployments do
not use this ledger.

```bash
uv run apx canary history --profile prod          # recorded releases, newest first
uv run apx canary history --profile prod --json   # structured metadata
uv run apx canary status --target apps --profile prod
```

Promote selects the canary recorded for the exact deploy label you pass
(`--canary-version`) and fails before deploying or tearing anything down when no
such canary is recorded. Promote and rollback re-deploy prod from the recorded
commit after verifying your checkout matches it — a rebuild, not restoration of
an immutable artifact. A rebuild can differ if dependencies, Bundle variables, or
workspace state moved.

The ledger is **recorded deploy metadata, not proof of the live App's runtime or
health**. Verify the deployed App with `apx status`, the App URL, and
`databricks apps logs <name>` before trusting a version claim in CI.

## Related

- [Upgrade apx-agent](upgrade.md) — bump pins safely (mismatch hard-fails deploy)
- [CI smoke test](reference/ci-smoke-test.md) — framework-repo Apps e2e (not consumer CI)
- [Deployment](deploy/overview.md) — deploying to Databricks Apps
