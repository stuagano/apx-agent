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

## Related

- [Upgrade apx-agent](upgrade.md) — bump pins safely (mismatch hard-fails deploy)
- [CI smoke test](ci-smoke-test.md) — framework-repo Apps e2e (not consumer CI)
- [Deployment](deployment.md) — Apps vs Model Serving
