# Disposable external-effect / worker-loss probe

**Danger: the first tool call deliberately terminates its worker with
`os._exit(86)`. Never install this on an existing/customer App.**

This is an opt-in, standalone experimental harness for bead `apx-aub`, not a
production APX host, new public API, or custom replacement Runtime Store. It
uses native `DurableAgentServer`, APX `compile_durable_handlers`, a managed
`DatabricksSessionStoreSaver`, and a deterministic tool-calling model. Only the
test tool implements external-effect deduplication.

The [dated deployed report](../../../docs/evidence/apx-aub-2026-11-20/README.md)
links exact SDK/code versions, invocation IDs, independent readbacks and teardown.
The resources named in that report **no longer exist**. Do not retry old parked
Apps or reuse an existing business sink.

## Preconditions and isolation

1. Obtain explicit operator approval for a **new** disposable App, managed
   Runtime/Session Stores, a dedicated auditable Workspace directory, synthetic
   writes, worker fault injection, replacement redeployment, readback and
   teardown. A source repository or task description is not that permission.
2. Select a valid U2M/M2M OAuth profile explicitly. Check `WorkspaceClient`'s
   `config.auth_type`; for U2M CLI OAuth use `databricks auth token --profile
   PROFILE` in memory and pass the access token through
   `apx_agent._obo.apps_inbound_oauth_headers`. Never log/save the token, use a
   PAT at Apps ingress, or treat ambient App credentials as user authority.
3. Choose a fresh deployment name, unique Session Store and unused sink
   directory. Verify absence before creation. The App's own principal needs
   `CAN_EDIT` on **only** the test directory. The operator needs independent
   read access to this sink and the managed Session Store. The tool runs as the
   declared **service identity**; no request-user recovery is attempted.
4. Use a separate staging directory with an empty `.gitignore` (not `*`). Copy
   `server.py` there. Build the current APX wheel (`cd python && uv build
   --wheel --out-dir STAGE`) and reference it in the staging project's
   `[tool.uv.sources]`. Do not package this probe in production APX.

The observed project used this minimal `pyproject.toml`; for a controlled rerun,
pin the Python/SDK versions from the evidence and record the actual installed
versions from **both deployed attempts**, not just a local venv:

```toml
[project]
name = "aub-disposable-proof"
version = "0.0.1"
requires-python = ">=3.12"
dependencies = ["apx-agent[agentbricks]", "uvicorn==0.42.0"]
[tool.uv.sources]
apx-agent = {path = "YOUR_BUILT_APX_WHEEL.whl"}
```

Use the native manifest with fresh names (placeholders must be replaced):

```toml
schema_version = 1
[agent]
framework = "langgraph"
server = "agentbricks"
deployment_name = "YOUR_DISPOSABLE_NAME"
[auth.user]
required = false
[session_store]
name = "YOUR_DISPOSABLE_SESSION_STORE"
```

The staging `app.yaml` command is `uv run --no-dev uvicorn server:create_app
--factory --host 0.0.0.0 --port 8000`. Set these explicit environment bindings:

| Variable | Meaning |
|---|---|
| `APX_AUB_SINK_ROOT` | New, dedicated Workspace directory |
| `APX_AUB_EFFECT_KEY` | Stable synthetic business key, unchanged across retry |
| `APX_AUB_ENABLE_CRASH` | Exactly `disposable-only`; otherwise startup refuses |
| `AGENT_SESSION_STORE` | The new managed Session Store from the manifest |
| `APX_DEV_UI` | `0` |
| `PIP_INDEX_URL`, `UV_INDEX_URL`, `UV_DEFAULT_INDEX` | `https://pypi.org/simple/` for the deployed App |

Run the installed native `agentbricks -p PROFILE deploy NAME --source STAGE`,
then grant only that new App principal access to the new sink. Let Agent Bricks
provision/inject Runtime Store coordinates and Session Store grants; do not copy
credentials, manually configure a shared Runtime Store, or redeploy another App.
The minimal SDK host does not expose `/readyz`; expect 404 there.

## Fault, independent readback and recovery

1. Generate one invocation UUID and one session ID. OAuth `POST /api/invocations`
   with `{"id": "UUID", "session_id": "SESSION", "background": true,
   "input": [{"role": "user", "content": "Record the disposable business fact"}]}`.
   Preserve the response and IDs; expect 202/queued.
2. The first tool attempt writes `attempt-1.json`, creates `effect.json` with
   `overwrite=False`, independently downloads its content, and inspects the
   managed checkpoint. It writes `boundary-1.json`, then hard-exits **before
   returning**. Wait for that boundary record, not just `run.started`.
3. Before replacement, the operator separately downloads `effect.json` and the
   boundary record. Read `DatabricksSessionStoreSaver(STORE,
   workspace_client=operator_ws).get_tuple(session_thread_config("aub-proof",
   SESSION, None))`. Expect a tool-call AI message, no matching tool result and
   no pending tool write. Preserve the checkpoint ID, metadata, message records
   and sink content; a final agent response is not independent evidence.
4. Explicitly redeploy **the same probe/App, business key and store bindings**
   to provide a replacement worker. Record the old/new SNAPSHOT IDs. Do not
   submit a new invocation to manufacture a recovery. Automatic worker capacity
   restoration is not assumed.
5. Poll `GET /api/invocations/UUID` for completed. Independently read
   `attempt-2.json`, `boundary-2.json`, `effect.json` and the managed checkpoint.
   Require the **same invocation/session/key**, attempt 2/recovery true,
   `created=false`, matching existing payload, and exactly one persisted tool
   result. Also retain `/api/invocations/UUID/events` for the original/recovery
   attempt markers. Report **raw tool attempts separately from business-file
   mutations**. Diagnostic JSON files are not business effects.
6. Resubmit the identical UUID/body, then read the sink again. Completion must
   remain unchanged without a third tool attempt. Do not change the body under
   the same invocation ID.

The sink uses one fixed file within a fresh per-experiment directory: the
payload embeds the stable business key. A conflict raises; it is not silently
accepted or overwritten. This is a targeted idempotency example, not a general
purpose production sink or a proof for arbitrary concurrent writers.

## Teardown and local validation

After capturing and sanitizing evidence, delete only the new resources:

```sh
agentbricks -p PROFILE deployments delete FULL_DISPOSABLE_APP_NAME --delete-source --yes
agentbricks -p PROFILE sessions stores delete DISPOSABLE_SESSION_STORE --yes
```

The first command verifies App ownership and removes its managed Runtime Store.
Delete the dedicated test Workspace directory recursively only within the
operator-approved scope. Verify App, Runtime Store, Session Store, uploaded
source and sink GETs return not-found. Do not delete shared branches or stores.
Retain token-free IDs, versions, hashes, readbacks, failure observations and
cleanup results. Link a dated committed artifact from the consumer ledger.

Local harness guards and evidence/source integrity checks:

```sh
cd python
uv run --frozen pytest -q tests/test_durable_effect_probe.py
```

Those tests validate the harness and committed record; they are **not** a new
live deployment or substitute for operator-observed fault injection.
