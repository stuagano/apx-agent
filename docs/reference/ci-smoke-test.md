# CI Smoke Test — `apps-smoke-test`

End-to-end CI workflow that catches regressions which only show up once the
agent is actually running inside a Databricks App. It deploys `memory_demo`
to an isolated per-PR app, hits the `/invocations` endpoint, asserts the
response, and tears down the bundle — every time.

## What it does

1. Builds the `apx-agent` wheel.
2. Stages `python/examples/memory_demo/` with the freshly built wheel into
   `.build/` (mirroring the manual deploy sequence).
3. Rewrites `databricks.yml` to use a per-PR app name so concurrent PRs
   never collide on the same workspace resource.
4. Runs `scripts/quickstart` to materialize an MLflow experiment + `.env`.
5. `databricks bundle deploy` then `databricks bundle run`.
6. Polls `databricks apps get` every 15s (up to 5 min) until
   `app_status=RUNNING` AND `compute_status=ACTIVE`.
7. `POST <url>/invocations` with a small Responses-API payload, asserts
   HTTP 200 + JSON `output` list with `>= 1` item.
8. Comments the deploy URL + result on the PR.
9. **Always** runs `databricks bundle destroy --auto-approve`, including on
   failure — see "Teardown" below.

## When it runs

- On every PR to `main` that touches (once the commented `pull_request`
  trigger is enabled):
  - `python/src/apx_agent/**`
  - `python/examples/memory_demo/**`
  - `.github/workflows/apps-smoke-test.yml`
- On-demand via `workflow_dispatch` from the Actions tab. Use this to
  retry a flaky run or to test workflow edits without pushing a PR.

PRs that don't touch the watched paths skip the smoke test by design —
docs-only PRs shouldn't burn DBUs.

This workflow is framework CI only (ephemeral `memory_demo`). Agent
project **dev / staging / prod** deploys live in scaffolded repos — see
[deploy-cicd.md](deploy-cicd.md).

## Required GitHub secrets

Provision under **Settings → Secrets and variables → Actions**:

| Secret | What it is |
|---|---|
| `DATABRICKS_CLI_CI_HOST` | Workspace URL, e.g. `https://example-workspace.cloud.databricks.com` |
| `DATABRICKS_CLI_CI_CLIENT_ID` | Service-principal OAuth client ID |
| `DATABRICKS_CLI_CI_CLIENT_SECRET` | Service-principal OAuth secret |
| `DATABRICKS_CLI_CI_USER_EMAIL` | SP's email — used as the MLflow experiment owner (`/Users/<email>/memory-demo-dev`) |

### Provisioning the service principal

1. Create a service principal in the workspace
   (Account console → Service principals → Add SP).
2. Generate an OAuth secret for it
   ([docs](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-m2m)).
3. Grant the SP:
   - `CAN_MANAGE` on the bundle workspace path
     (`/Workspace/Users/<email>/.bundle/...`).
   - `CAN_QUERY` on the LLM serving endpoint
     (default `databricks-claude-sonnet-4-6`).
   - Workspace-level "Databricks SQL access" + "Allow cluster creation"
     entitlements (the App runtime needs them).
4. Store the four values as repo secrets above.

## Per-PR isolation — app name

App names are computed at runtime as `md-pr-<suffix>`, where `<suffix>` is
the last 22 chars of `github.event.number` (or `github.run_id` for
manual dispatch). That keeps the name under the 30-char Databricks App
limit while guaranteeing uniqueness across PRs running in parallel. The
`databricks.yml` `bundle.name`, the app resource key, and the app's
`name:` field are all rewritten in a single Python step so the deployed
bundle is fully isolated from any other run.

## Teardown guarantee

The final step uses `if: always()`, so `databricks bundle destroy --auto-approve`
runs on success, failure, cancellation, and timeout. Destroy is wrapped to
emit a warning instead of failing the job a second time if it returns
non-zero — that way the original failure stays surfaced in the logs.

If destroy ever fails, the workflow logs the SP's profile + the app name;
you can clean up manually with:

```bash
databricks apps delete <app-name> --profile ci
databricks bundle destroy --target dev --auto-approve --profile ci
```

## Debugging a failed run

1. Open the PR — the failure comment links to the workflow run.
2. The poll step prints `app_status` + `compute_status` every 15s. If it
   timed out, scan for the last status: `ERROR`, `STOPPED`, or stuck on
   `STARTING` for >5 min usually means the SP is missing a permission.
3. The invoke step prints the first 2 KB of the response body. A 401 or
   403 means the OAuth token didn't authorize against the deployed app
   (check that the SP owns the app or has CAN_USE on it).
4. Check `databricks apps logs <app-name> --profile ci` for runtime
   stack traces.

## Cost

Each run provisions one Databricks App for the duration of the smoke
test (typically 3-5 minutes deploy + 30s invoke + teardown). At the
default app compute size that's a few cents per PR. The shared LLM
serving endpoint is billed by token; the smoke test sends one short
prompt and reads one short completion.

## Skipping the smoke test

Don't touch the watched paths. Edits to `docs/`, `typescript/`, `hub/`,
or other Python examples won't trigger this workflow. If you need to
land a known-bad change with the smoke test red, push to a feature
branch and merge with admin override after a follow-up green run.
