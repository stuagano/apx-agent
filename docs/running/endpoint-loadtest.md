# Load-test a durable agent

[ADR 0001](../adr/0001-build-on-agent-bricks.md) routes load testing to the
upstream command `agentbricks endpoint loadtest` (Locust). A durable apx host
serves `POST /api/invocations`. There is no apx wrapper. The optional Locust
dependency is the upstream extra `databricks-agentbricks[loadtest]`
(`locust>=2.31`). This page records one short run against a disposable app.
It is not a capacity number.

## Command

Authenticate with a Databricks workspace profile that is user OAuth, not a
PAT. Pass the app name (not only `--url`) so the CLI resolves the app and
injects OAuth. `--auth` makes that explicit. The profile used below was
`fevm-stable` (`databricks auth describe` reported `auth_type: databricks-cli`).
The CLI has no token flag on this command. `{{request_id}}` is a fresh
invocation id; `{{user_id}}` is stable per simulated user.

```bash
agentbricks -p fevm-stable -o json endpoint loadtest \
  agent-bricks-apx-dur-gen \
  --path /api/invocations \
  --auth \
  --requests requests.json \
  --concurrency 2 \
  --max-duration 20 \
  --max-requests 4 \
  --timeout 45
```

Warm-up is on by default (`--warmup` / `--no-warmup`). A persisted invocation
failure answers HTTP 502. That status fails warm-up, and the CLI exits before
Locust sends a measured request. Add `--no-warmup` when the sample you want is
that failure. The run recorded below omitted it.

`requests.json` for this run was one body, repeated by the CLI:

```json
[
  {
    "id": "{{request_id}}",
    "session_id": "apx-9pc-{{user_id}}",
    "input": {
      "messages": [
        {"role": "user", "content": "Reply with the single word pong."}
      ]
    }
  }
]
```

The body does not set `stream`, so this run did not pass `--sse`. Upstream's
own example pairs `--sse` with `"stream": true`.

## Recorded run

| | |
|---|---|
| CLI | `agentbricks` 0.10.0, Locust 2.47.0 |
| Start | 2026-11-21T01:25:33Z |
| End | 2026-11-21T01:25:45Z |
| App | `agent-bricks-apx-dur-gen` |
| URL | `https://agent-bricks-apx-dur-gen-7474660401027952.aws.databricksapps.com` |
| Snapshot | `01f1e546718010678017040e46a17107` |
| Configured load | 2 users, 20 seconds, 4 measured requests, 45 second timeout |
| Exit code | 1 |

Locust was not installed in the project environment. It was imported from a
side directory (`PYTHONPATH`) for this run only. The exact argv, the CLI
stderr, and the app status are in
[endpoint-loadtest-2026-11-21.txt](endpoint-loadtest-2026-11-21.txt). No
token or `Authorization` header is in that file.

What the CLI printed:

```text
ERROR:root:Load test warm-up failed: HTTP 502
{
  "error": {
    "message": "Load test warm-up failed: HTTP 502. No measured requests were sent."
  }
}
```

Stdout was empty. `--report-dir` stayed empty: no `summary.json`, no Locust
CSV, no status-code histogram. The only load-test status code observed was
that one warm-up HTTP 502. Measured requests: 0. The configured 2 users and
20 seconds never started.

`databricks apps get agent-bricks-apx-dur-gen -p fevm-stable` immediately
before (2026-11-21T01:25:32Z) and after (2026-11-21T01:25:45Z):

- `app_status.state`: `RUNNING` (`App is running`)
- `compute_status.state`: `ACTIVE` (`App compute is running.`)
- active deployment: `01f1e546718010678017040e46a17107`, `SUCCEEDED`

The app did not go `CRASHED`. A second, larger run was not started.

The app log for that request (client IP omitted) recorded
`Invocation execution failed: 449ddee9-1740-48ff-afc7-72271a808e61 attempt=1`
and then `POST /api/invocations` `502`. The exception text was
`databricks.sdk.errors.platform.ResourceAlreadyExists`:
`/Shared/apx-aub-1121-sink/attempt-1.json already exists`.

After the load test had stopped, one read
`GET /api/invocations/449ddee9-1740-48ff-afc7-72271a808e61` returned HTTP 200.
The body was `status: failed` with `error.code` `UPSTREAM_ERROR`,
`upstream_code` `RESOURCE_ALREADY_EXISTS`, and `upstream_status` 409. That
GET is not part of the load-test counts. The same `apps get` after the GET
was still `RUNNING` / `ACTIVE` on the same snapshot.

## Contract gap

Filed as https://github.com/stuagano/apx-agent/issues/1207.

**Title:** Durable invocation failure is HTTP 502, so agentbricks loadtest records no sample

**What happened:** On 2026-11-21T01:25:33Z, `agentbricks` 0.10.0
`endpoint loadtest` against `agent-bricks-apx-dur-gen` (snapshot
`01f1e546718010678017040e46a17107`) sent one warm-up
`POST /api/invocations` and exited 1. stderr:
`Load test warm-up failed: HTTP 502. No measured requests were sent.`
No Locust summary was written. `databricks apps get` stayed `RUNNING` and
`ACTIVE`. The app log showed the invocation
`449ddee9-1740-48ff-afc7-72271a808e61` failing in a tool upload because
`/Shared/apx-aub-1121-sink/attempt-1.json` already exists. A later GET of
that id returned HTTP 200, `status: failed`, `error.code: UPSTREAM_ERROR`,
`upstream_status: 409`, `upstream_code: RESOURCE_ALREADY_EXISTS`.

**Expected:** The server keeps the invocation it already stored as `failed`.
The POST stays HTTP 502 with that body. GET of the same id stays HTTP 200.
The load-test report counts the 502 as an error. Default warm-up exits before
any measured request, so that count needs `--no-warmup`. OAuth and
`POST /api/invocations` themselves worked. The missing piece is a measured
sample of this failure.

## Counting the persisted failure

The 502 is the app answering, with the app still `RUNNING`. AgentKit's
`describe_failure` (agentbricks 0.10.0) maps a Databricks SDK error onto
`error.code: UPSTREAM_ERROR` and HTTP 502, and copies the upstream status onto
the error (`upstream_status` 409 and `upstream_code: RESOURCE_ALREADY_EXISTS`
for this conflict). A `ValueError` stays HTTP 500 `AGENT_ERROR`. The POST body
is `{"id", "status": "failed", "error": ...}`. `GET /api/invocations/{id}`
returns HTTP 200 with the same body. Both shapes are pinned by
`test_native_execution_failure_remains_persisted_500` and
`test_native_sdk_conflict_remains_persisted_502` in
`python/tests/test_native_invocation_contract.py`.

`agentbricks endpoint loadtest` treats every status outside 200–299 as
`StreamError` and drops the body. A 2xx body is a successful sample even when
its `status` is `failed`. The default warm-up request runs that check before
Locust starts. On failure the runner exits with `No measured requests were
sent`, and `summary.json` stays unwritten.

`--no-warmup` skips that request. Each measured 502 then lands in
`summary.json`: `counts.failed` increments, and `errors` has a key `HTTP 502`.
`workload.warmup_requests` is 0. The app can stay `RUNNING` / `ACTIVE` for the
whole run. That report counts this failure. It is still not a capacity number.

Teaching warm-up to treat a JSON body with `status: failed` as "the endpoint
answered", while the same response still counts as an error, belongs in
upstream `agentbricks`. A Locust wrapper stays out of this repo
([ADR 0001](../adr/0001-build-on-agent-bricks.md)).

#1207 stays open until a new run records that sample: a disposable app, a tool
that fails and persists the invocation, argv including `--no-warmup`, a
non-empty `counts.failed` (or `errors`), and `databricks apps get` still
`RUNNING`. Put the sanitized output next to
[endpoint-loadtest-2026-11-21.txt](endpoint-loadtest-2026-11-21.txt).
