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

## Ready-to-file issue

Not filed. `gh issue create` was not run.

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

**Expected:** A durable invocation the server has already stored as `failed`
should show up in the load-test report as a counted error. HTTP 502 from a
still-running app should not abort the run before any measured request.
OAuth and `POST /api/invocations` themselves worked; the missing piece is a
measured sample of this failure.
