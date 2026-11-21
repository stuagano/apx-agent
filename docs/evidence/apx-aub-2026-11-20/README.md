# apx-aub: replay-safe external effect across deployed worker loss

**Observed 2026-11-20, UTC.** [Sanitized machine-readable evidence](evidence.json).
[Fault-injection source](../../../scripts/probes/apx_aub/server.py) ·
[Reproduction / safety procedure](../../../scripts/probes/apx_aub/README.md) ·
[Local harness tests](../../../python/tests/test_durable_effect_probe.py).

## Result and scope

The **same deployed invocation** executed its tool twice across a hard process
exit, while the dedicated sink received **one effective business mutation**.
Recovery completed with one persisted tool result. The sink's stable business
key and create-only operation provide deduplication; APX does **not** provide
exactly-once tool execution or exactly-once arbitrary external effects.

This closes the external-effect experiment for `apx-aub`, not the other five
#864 live items, request-user recovery, or consumer retirement. No production
runtime machinery or compatibility consumer was changed or removed.

## Authorization, deployment, code and identity

The operator explicitly authorized a new disposable deployment, test-only writes,
worker fault injection, independent sink/store access, replacement redeployment,
and teardown in `fevm-stable`. Pre-existing Apps, parked
`agent-bricks-stu-dur-1002`, and the historical `864probe` were not used.

- Workspace `7474660401027952`, profile `fevm-stable`.
- App `agent-bricks-aub-1120-2018`, space-less **MEDIUM**, single worker.
- Runtime Store `agent-bricks-aub-1120-2018`, schema `databricks_agentkit_runtime`;
  provisioned and deleted by the native Agent Bricks CLI, not custom SQL.
- Session Store `apx-aub-1120-2018-sessions`, UID
  `d04f9a29-fa35-4c56-ad7f-d545222102fb`.
- Auditable sink `/Shared/apx-aub-1120-2018-sink/verified`; the new parent
  directory granted only the test App principal `CAN_EDIT` (plus operator
  ownership). The sole business record is `effect.json`; attempt/boundary JSON
  files are diagnostics, not business mutations.
- Ingress used an explicit U2M OAuth access token from `databricks auth token
  --profile fevm-stable`, passed through `apps_inbound_oauth_headers`. No PAT,
  stored token, or ambient App-credential fallback was used as user authority.
- The **tool** deliberately ran as the test App service principal
  `69440258-78c8-4e60-b74e-d9ab3da4cf67`. The native manifest declares
  `[auth.user] required = false`; this is not an OBO/request-user recovery proof.

The probe registers APX `compile_durable_handlers(..., recovery=True)` with the
SDK's `DurableAgentServer` for invoke/recover, a real managed Session Store saver,
and a deterministic `BaseChatModel`. There is no paid model call or production
business integration. It is a fault-injection harness, **not** the stock APX web
launcher or a full customer-consumer equivalence proof.

APX wheel `0.7.8.dev8+g0c15c9f84` was built from base commit
`0c15c9f8464e62418de9d9244721764b54e0b990`; the standalone probe is additional
source, not part of that wheel. Its SHA-256, wheel SHA-256 and deployment-manifest
hashes are retained in `evidence.json`; the integrity test checks the committed
probe against the source actually deployed. Both deployed attempts recorded:

| Component | Deployed version |
|---|---|
| Python | 3.15.1 |
| databricks-agentbricks / AgentKit runtime | 0.10.0 |
| databricks-sdk | 0.169.0 |
| langgraph | 1.2.24 |
| langgraph-checkpoint | 4.3.2 |
| langchain / langchain-core | 1.5.2 / 1.8.4 |

These are **in-App** versions, not an assumption based on the local test venv.

## Exact crash and replacement boundary

Invocation `c8dc5bd7-fddf-43e7-a1b5-228aec67fa2d`, session
`aub-session-b523f4c2-0dd0-4869-9589-acdba3bbdd4f`, fixed business key
`aub-business-20261120-2018-verified`, fixed tool-call ID `aub-call`.
Attempt identity is `(invocation_id, attempt)`; the runtime exposed attempts
**1** (original) and **2** (`is_recovery=True`), not separate opaque attempt UUIDs.

1. Initial SNAPSHOT `01f1e521524c15f582729feed29b057c` succeeded at
   `20:31:18Z`. `POST /api/invocations` returned **202 / queued**.
2. At `20:32:07Z`, attempt 1 called `workspace.upload(effect.json,
   overwrite=False)` successfully with the harmless synthetic payload. The tool
   downloaded and compared it, then read the managed checkpoint independently.
3. Checkpoint `1f1e5217-42c7-6071-8001-e18ec7b5495c` contained `human, ai`
   (the AI tool call), **no ToolMessage and no pending tool write**. The tool
   persisted `boundary-1.json`, then called **`os._exit(86)` before returning**.
   Thus neither the tool result nor the following agent checkpoint could commit.
4. Before replacement, the OAuth operator independently downloaded the boundary
   and `effect.json`, and called `DatabricksSessionStoreSaver.get_tuple` against
   the managed Session Store. The checkpoint still contained no tool result;
   the external write was already durable. Invocation HTTP readback returned
   **502**, consistent with the dead sole worker.
5. The operator explicitly redeployed the **same source and App with the same
   store bindings**: SNAPSHOT `01f1e52195b019d292e3066703216015`, successful at
   `20:33:11Z`. This—not an unobserved automatic App restart—supplied the
   replacement worker. No second invocation was submitted to cause recovery.
6. At `20:33:17Z`, attempt 2 recovered the original invocation/session and
   repeated the create-only write using the same business key. The sink returned
   `ResourceAlreadyExists`. The tool independently compared the existing payload
   and returned `created=false`, allowing the tool-result checkpoint to commit.

The crash is deliberately *inside* the tool, not after a returned result or a
completed invocation. The first attempt performs an ordinary successful write;
only the first attempt's post-write boundary triggers the hard exit.

## Independent final readback

The operator—not merely the agent's final message—read these after replacement:

- HTTP `GET /api/invocations/{id}`: **200 / completed**, tool result
  `{"created": false, "effect_key": "aub-business-20261120-2018-verified"}`.
- Managed Session Store: checkpoint
  `1f1e5219-e94c-6f67-8003-8c54792e9959`, messages `human, ai, tool, ai`,
  exactly one matching `ToolMessage`, no pending write channels.
- Workspace sink: exactly `attempt-1.json`, `boundary-1.json`,
  `attempt-2.json`, `boundary-2.json`, and **one `effect.json`**. Its value was
  unchanged from the independent post-crash readback. The Workspace listing
  records one create/modified timestamp for this business file; the tool never
  uses overwrite for it.
- Persisted HTTP event history: original and recovered `aub.attempt` events
  identify attempt 1 / recovery false and attempt 2 / recovery true, followed by
  `run.completed`. Events corroborate, rather than replace, the sink/checkpoint
  proof.
- Reposting the identical invocation ID/body returned **202 / completed**.
  The subsequent sink listing still had the same five files, without attempt 3.

**Raw tool attempts: 2. Effective business mutations: 1.** The three other
notions—model steps, diagnostic writes, and invocation submissions—are not
counted as business mutations. This result also occurred in a pilot with a
separate synthetic key before the version-stamped run; only the final run's
complete record is the acceptance artifact.

## Observed failures, teardown and residual limits

- The first source sync had an all-files `.gitignore`, uploaded zero files and
  failed deployment. Removing that staging-only ignore rule fixed it. Reported
  separately as Beadhive escalation **hq-xal**; no deployment code was changed
  to hide it.
- The minimal SDK host returns **404** at `/readyz` and `/`; it does not mount
  APX's web/readiness routes. No `durable:true` claim is used as proof.
- A sole worker returned **502** after the injected exit. Replacement here was
  an explicit redeploy, not proof of automatic capacity restoration, replica
  failover, idle scale-down/wake, or a recovery-latency guarantee.
- The effect key must survive retries, the sink must enforce create-only
  uniqueness, and an existing key's payload must match. A conflicting payload
  raises rather than being accepted as a successful deduplication. A mutable or
  non-idempotent downstream API needs its own idempotency/reconciliation design.
- There is no distributed transaction between the external sink and checkpoint
  store. Another failure before a result checkpoint may cause more attempts.
  The deterministic harness does not establish guarantees for every real tool,
  arbitrary concurrency, approval principals, trace correlation or downstream
  OBO. Request-user automatic recovery remains unsupported.

**Teardown verified at `2026-11-20T20:35:34Z`:** native deployment deletion
removed the App, its managed Runtime Store and uploaded source. The dedicated
Session Store was separately deleted; the whole newly created sink directory
(including pilot files) was removed. Independent Apps, Runtime Store, Session
Store and Workspace GETs all returned `NOT_FOUND` / `RESOURCE_DOES_NOT_EXIST`.
The shared platform Lakebase branch and every pre-existing App were outside the
cleanup target. Local sanitized evidence remains; no live test App/store/sink
is intentionally left running.

## Repository validation

- `env -u OTEL_SDK_DISABLED make check`: **6,122 passed, 13 skipped**;
  no new skip markers or disabled telemetry. The existing public-artifact
  hygiene check caught an unnecessary private customer reference in a draft of
  this report; the reference was removed and the **full** gate rerun successfully.
- `UV_FROZEN=true uvx --offline pre-commit run --all-files`: all configured
  hooks passed. Frozen dependency resolution prevents helper commands from
  rewriting registry metadata; no hook is skipped or weakened.
- Five probe tests cover write/exit/deduplicated recovery, conflicting payload,
  wrong-key/already-checkpointed guards, deterministic model behavior and the
  committed evidence/source hash and boundary invariants. These tests validate
  the harness and artifact integrity, not a fresh live run.
- A separate focused code/evidence review found no blockers, while explicitly
  retaining the residual limits above.
