# Changelog

All notable changes to apx-agent. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are git tags
(`v*`) and the wheel version is derived from the tag via hatch-vcs.

## [Unreleased]

Headline: apx declarations compile to the native Databricks durable agent
runtime, which is now the only Apps host. Composite and A2A agents run on it,
and managed sessions and memory replace app-owned state on that path.

### Added

- **Existing-agent registration from its card (#834).** Hub registration keeps the crawled card's `instructions` and `tools` (skills stay the fallback), and copies the response `traceparent` onto the card as `trace_header`. `RemoteDatabricksAgent` uses those card fields the same way.
- **Native durable runtime.** Agent declarations compile to a native MLflow
  `ResponsesAgent` or `DurableAgentServer`. One declaration selects the runtime,
  App Space and managed session binding. Deploys hand store reconciliation to
  the Agent Bricks CLI. New Apps scaffolds default to `durable_agent_server`;
  `--runtime responses_agent` keeps the compatibility layout (#865–#867, #877).
- **Composite and A2A agents on the durable runtime.** `KeywordRouter`,
  `SequentialAgent` and A2A peers compile to `durable_agent_server` (#900).
  Durable recovery traces join on the invocation id (#920).
- **`apx-agent doctor --durable`.** A readiness check that runs the native
  runtime rather than reading config. It follows the same deploy routing as
  App Space projects (#907, #908).
- **Apps API deploy path.** `[tool.apx.agent.deploy] backend = "apps_api"`,
  with `entrypoint`, `include` and `env`, deploys with get-or-create steps and
  keeps the declared app name. Use it for `mcp-` names that the Agent Bricks CLI
  would rename (#913, #915).
- **Agent identity (#816).** `[tool.apx.agent] identity` declares the agent's own
  principal: `"sp"`, `"obo"` or `"named-sp:<name>"`. Tools that declare no
  identity inherit it; a per-tool `identity` still wins. `apx-agent deploy`
  reports the agent principal and names each operation's principal in the
  authorization summary. A tool call's span carries `apx.principal` (the
  principal it ran as) and `apx.user.hash` (the user whose request triggered
  it). A declared name is the App's own service principal. A Databricks App
  holds one such credential, and the declaration does not create a second
  one (#925).
- **Per-caller MCP tool allowlist.** `callers = [emails]` on a tool hides it
  from other MCP callers, and `/api/tools/<name>` returns 403 `caller_denied`.
  A request without a caller email is denied (#929).
- **Declared session-to-memory extraction.** `[tool.apx.agent.memory.extraction]`
  stays off until `enabled = true` on a managed memory store with a managed
  session store. `apx memory extract --session-id` distills that one completed
  session through AgentKit. `--dry-run` returns the entries without writing,
  and a real write is read back before it is reported. The session is not
  rewritten, and existing-memory consolidation is unchanged (#928).
- **Lakebase → managed memory import.** `inventory_memory_table` and
  `import_memories` copy a Lakebase memory table into a managed memory store.
  Source rows are left in place, and an interrupted import can be re-run
  (#927).
- **Managed state contract map.** [docs/design/managed-state-contract-map.md](docs/design/managed-state-contract-map.md) records the AgentKit 0.4.0 / SDK 0.146.0 methods this checkout can call and maps MemoryStore, ConversationStore, session state and checkpoints onto them. A `fevm` probe on 2026-10-13 found managed memory and session stores enabled (Beta, Lakebase-backed) and completed a disposable `add`/`get`/`recall`/`update`/`delete` plus an actor isolation check. Retention and measured cost remain unknown (#854, #924).
- **Doctor names services that need a guardrail-policy check.** It lists the
  bound model service and every `service_policies` target to confirm in the
  Unity Gateway UI. No API can read these attachments back yet (#917).
- **Instruction optimization for named child agents** (#852).

### Changed

- **Native CLI vocabulary (#862).** `--target` is the hosting destination; `[tool.apx.agent].target` is the runtime. Native deploy/destroy accept `--environment` as an alias of `--bundle-target`. Doctor reports destination and declared runtime separately. Generated native dependencies stay `apx-agent[eval,agentbricks]`; `apx-agent[apps]` was never an extra, so run/doctor now tell you to install `uvicorn[standard]`. Example projects that still asked for the nonexistent `apx-agent[langgraph]` extra now use published extras (`eval`, plus `agentbricks` where the durable runtime is declared).
- **Managed store live acceptance (#855/#856).** A disposable provision/CRUD/isolation probe on profile `DEFAULT` (`fevm-serverless-stable-7def3z`) succeeded and deleted its stores. Opt-in re-run: `APX_MANAGED_STATE_LIVE=1`. Retention, grants, and App-process startup remain open.
- **Breaking: the Apps host is `DurableAgentServer` only** (#898, #926, #930).
- **Breaking: the TypeScript agent runtime is retired.** The internal AppKit
  host and Python tool bridge are removed. Examples run on the native server.
  The browser TypeScript clients and the Python `ResponsesAgent` target
  remain (#873, #879).
- **The chat LLM goes through AI Gateway only.** `get_llm` always uses the
  gateway, and Apps no longer grant `CAN_QUERY` on the chat model. Legacy
  external-model chat endpoint provisioning is removed (#861, #872, #874).
- `databricks-agentbricks` bumped to 0.5.0 (#922).

### Fixed

- **A2A Apps peers keep the request-user token (#903).** A Databricks Apps
  peer (`apps/<name>`) with a request-user token no longer takes the Apps SDK
  transport, whose auth hook signs the call as the calling app's service
  principal. The call goes over direct HTTP with `X-Forwarded-Access-Token`
  and `Authorization: Bearer`. Supervisor long-task continuation still uses
  the SDK, because the HTTP path cannot resume a paused task.
- App Space deploys create a missing declared managed session store and grant
  the app WRITE. Composite agents no longer require a session in preflight or
  the manifest (#905, #910, #912, #916).
- Every Apps resource kind emits the required resource-level `name` (#813, #923).
- The App Space scope check names the tool and the admin (#815, #918).
- Native runtime stores are cleaned up before App teardown (#871). Deploy dry
  runs report model validation errors (#875). Gateway status is named when
  `/readyz` is not JSON (#878).

### Docs

- How a durable agent operates, App Space vs serverless runtime vs horizontal
  scaling, where managed stores live, and the native execution boundary
  (#880–#897).

## [0.7.1] — 2026-10-01

Headline: per-hop user OBO as the A2A trust boundary, typed sequential
chains, model comparison in evaluation, and declared UC ABAC governance.

### Added

- **Typed sequential chains.** Agent output is validated before a sequential
  handoff. Structured payloads survive remote steps, and failure escalation
  is opt-in (#843, #844, #846).
- **Evaluation.** Evaluate typed chains with recorded tool results, compare
  quality, latency and cost across models, and a simpler customer evaluation
  path (#848, #849, #851).
- **Declared UC ABAC via `[tool.apx.agent.data]`.** Default `apx.agent.*` tags
  go on auto-created agent tables, with UC-legal tag keys.
- **Dev UI.** The hub folds into the `/_apx` dev UI under one caller-OBO auth
  model (#825). The dev UI and Agent Hub are reskinned to Du Bois.
- **Tracing.** Root spans carry `mlflow.trace.session` for per-session grouping.
  Harness and version stamps are folded into `safe_span`.
- **Ontos governance adapter** under `adapters/ontos` (#811, #812).

### Changed

- Per-hop user OBO is the documented and asserted A2A trust boundary (#814,
  #829).
- CI runs the suite on Python 3.13 and drops the 3.11 leg (#828).

### Fixed

- Apps deploy-state write failures are reported instead of swallowed (#823,
  #824). Canary reports unmatched app-name traces as unknown (#822).
- Remote HTTP fallback is kept for SDK setup errors (#845). Step deadlines
  work on Python 3.10 (#847).
- Scaffold no longer emits the unsupported `experiment` app resource, and
  emits a valid `serving_endpoint` bundle shape.

### Docs

- Day-2 upgrades, versioning and rollback (#820). Agent governance for Unity
  Catalog data architectures (#819). Agent mesh design.

## [0.7.0] — 2026-09-23

Headline: compiled Apps authorization and per-tool identity, declarative A2A
agent graphs, session budgets, horizontal-scaling readiness, and a much
richer evaluation loop in the dev UI.

### Added

- **Apps authorization compiler.** Infers each tool's execution identity (user
  OBO vs App service principal) and compiles an identity-partitioned
  `AuthorizationPlan` checked at deploy (#712).
- **Tool-scoped auth.** Per-tool identity and a UC/secret permission ceiling,
  enforced at compile time and runtime (#758).
- **Declarative A2A agent graphs + control-result protocol.** Declared leaves
  bind to remote Apps over A2A. Remote `LoopAgent` bodies and `HandoffAgent`
  peers route control the same way the in-process path does (#777).
- **`session_budget`.** A cumulative per-session token cap on served paths
  (#780).
- **Horizontal-scaling readiness.** A guardrail refuses scaled deploys that
  use in-memory sessions. `[tool.apx.agent.deploy]` declares scaling, and
  `compute_min_instances` / `compute_max_instances` are emitted into
  `databricks.yml`. `APX_DECLARED_INSTANCES` is kept for the runtime guard's
  read-back. These fields need a Databricks CLI/bundle schema new enough to
  accept them (#779, #804).
- **Distributed tracing across A2A hops.** `inject_tracing_headers` (client)
  and `continue_trace_from_headers` (server) carry MLflow tracing context
  across inter-agent HTTP calls. A supervisor and its remote specialists then
  share one trace, with the specialists' spans nested in the caller's trace.
  OpenTelemetry `traceparent` alone does not do this with MLflow 3.x. Both
  helpers do nothing when MLflow is absent (#754).
- **Declared external-model endpoints** (`model = "bedrock:…"`) (#773, #774).
- **Runtime.** A deferred `tool_search` router on the compiled path (#789),
  and context isolation for `ParallelAgent` (#782).
- **`document_extract` tool factory.** Compiles parse → extract (#796).
- **Governance.** `dqx_quality_score` on decision spans (#807), and an opt-in
  live proof for the runtime quality gate (#809). The ABAC contract covers
  remote hops (#755).
- **Observability.** A harness-version trace attribute and `apx-agent status`
  (#766).
- **Tool inspector at `/_apx/tools`.** `/_apx/tools` is a working inspector
  again, not a 302 to Edit. It shows live source from `inspect.getsourcefile`,
  saves in place, and runs tools via `/_apx/replay/tool`. It is also a nav tab,
  the Chat shell's Tools tab, and an Edit-page Schemas/Tools sub-tab. Tools,
  Chat and Trace share one table renderer. New routes: `GET /_apx/tools/list`,
  `GET /_apx/tools/file`, `POST /_apx/tools/save` (#806).
- **Dev UI evaluation loop.** Thumbs feedback with rationale on traces, judge
  alignment on the Eval page and in the Chat shell, and MemAlign run history.
  Also a side-by-side trace diff, per-trace token cost, per-tool fail rate, a
  p50/p95 latency sparkline, auto-eval of chat responses, and an "Improve
  instructions" (GEPA) button (#713–#753, #765, #790–#794).
- **Examples.** PE55 Brickfood support agent, and a SequentialAgent 10-K demo
  (#797–#799).

### Changed

- **`executor="claude-sdk"` renamed to `executor="openai-compat"`** (#833).
  It never was Anthropic's Agent SDK — it is an OpenAI-compatible tool loop on
  Databricks Model Serving. `ClaudeSDKExecutor` is now `OpenAICompatExecutor`.
  The old value and class name still work; the value emits a `FutureWarning`.

- **Breaking: the watchdog governance surface is now `governance`.** Runtime
  names dropped the companion repo: `_watchdog.py` → `_governance.py`,
  `WatchdogClient` / `WatchdogGuard` / `WatchdogDecision` → `Governance*`,
  `make_watchdog_transport` → `make_governance_transport`, CLI group
  `apx-agent watchdog` → `apx-agent governance`, env vars `APX_WATCHDOG_*` →
  `APX_GOVERNANCE_*`. Trace/audit attributes `apx.watchdog.*` became
  `apx.governance.*`, and the trace-export columns `watchdog_action` /
  `watchdog_policy_id` became `governance_action` / `governance_policy_id`.
  Migrate existing dashboards and `WHERE watchdog_action IN (...)` queries.
  The external companion repo is now
  [stuagano/databricks-agent-governance-monitoring](https://github.com/stuagano/databricks-agent-governance-monitoring)
  (#803, #805).
- New scaffolded agents default to the AppKit host (#710).
- PR CI runs only the runtime core plus tests mapped to changed paths (#783).

### Fixed

- Eval scores the final assistant message (#801). Labeling reports traces that
  `get_trace` dropped instead of skipping them silently (#764).
- `ScopeGuard` refreshes after late wiring (#784). GEPA candidates no longer
  have `$` mangled (#785).
- Agent queries in the CLI are secured and rendered (#775). Trace feedback
  diagnostics are safe to expose (#776). Genie gives a clear error on an empty
  `space_id` (#739).

## [0.6.0] — 2026-08-31

Headline: Apps deploy through the AppKit host by default, trace-linked
feedback, and UC-backed trace observability as the Apps default.

### Added

- **AppKit host for Apps deployments** (internal). It is the default, with
  governance adapter, tool bridge, dev toolbar and APX limits projected into
  agent options. `APX_APPS_HOST=python` remains as a fallback (#688, #689,
  #691, #692).
- **Trace-linked feedback.** An MLflow feedback helper, CLI commands and a
  per-app API, with idempotency metadata, plus a feedback adapter example
  (#697–#702).
- **Runtime tool mutation** (#686).
- **UC-backed trace observability as the Apps default.** Experiments bind to
  UC traces, and the declared trace warehouse drives grants (#684).
- AppKit PLG discovery example app (#693).

### Fixed

- Unity Catalog function signatures are baked at build time (#695). Empty
  Claude tool arguments are normalized (#694). The per-app runtime agent name is
  honored (#703).
- PyPI proxy support is safe for the public repo (#708).

### Docs

- Judge calibration, sampled production scoring, trace-linked feedback, and a
  human issue triage loop (#700, #704–#706).

## [0.5.0] — 2026-08-26

Headline: service policies, YAML-compiled agent projects, Agent Bricks OBO
interop, and a discovery workflow.

### Added

- **Service Policies.** Declarative built-in, LLM-as-a-judge and SQL policy
  attachments. Includes local enforce/dry-run evaluation, deterministic rank
  ordering, MCP-only ASK handling, audit metadata and YAML/project generation.
  The CLI has plan/apply/verify boundaries. Native apply/verify stay explicit
  plan-only operations until a verified Databricks attachment API exists
  (#680).
- **Agent projects from YAML specs.** Graph specs for agents and the root
  compile to `agent.py`, and the generated projects are retained (#663, #664,
  #666).
- **Agent Bricks OBO interop.** Knowledge Assistant and Genie subagents, with
  Responses-API Knowledge Assistants declared as config tools (#667, #669).
- **Discovery.** A generic tech-to-business PoV discovery workflow (#675).
- **Dev UI.** An OKF Knowledge tab, a reusable semantic overlay for topology,
  runnable example workflows, and agent flow graph tooling (#679, #681, #683).
  The compact trace shows sub-agents (#671).
- OKF bundles for the bakehouse and HubSpot DataAgent examples.

### Fixed

- OBO identity is kept across the A2A fallback (#682).
- Served responses take visible text from thinking-model content, not raw JSON.
  Contextvars propagate into the stream worker thread.
- Trace views show only real sub-agents and hide per-token events (#672, #673).
- Deploy strips every Databricks PyPI-proxy variant from `uv.lock` (#668).

## [0.4.8] — 2026-08-10

Headline: safer multi-agent discovery and identity handling, more reliable
scaffolding and chat responses, and clearer state-sharing and grounding docs.

### Fixed

- **Multi-agent security and routing.** Apps discovery now requires caller
  identity, rejects unsafe peer URLs and binding names, fails closed when
  gateway identity is unavailable, and binds A2A task state to the OBO
  principal (#610–#614, #617, #628–#631, #636).
- **Composition and guardrails.** Handoff descriptions, routing descriptions,
  guardrail config propagation, and explicit `max_iterations=0` behavior now
  compile consistently (#616, #624, #632–#635).
- **Scaffold and chat reliability.** Scaffold catalog/schema selection can be
  entered manually when the picker is insufficient, and the Hub extracts
  assistant text instead of rendering raw response payloads (#657, #660).

### Added

- **Dev UI and topology workflows.** Workspace discovery, topology wire/chat,
  OKF pack generation, and sample DataAgent fleet support (#594–#595).
- **Evaluation and diagnostics.** CLEARS-aligned scorers and an AI Gateway
  guardrail warning in `apx-agent doctor` (#596).

### Docs / examples

- Documented grounding asset lifecycle, `Dependencies.State` and
  `output_key` state sharing, and the identity boundaries for remote agents.
- Hardened the example agents and added ADK-pattern skills and audit notes
  (#597–#608).

## [0.4.7] — 2026-08-04

Headline: workspace discovery + topology wire/chat in the Apps Dev UI, remote
long-task continuation, CLEARS-aligned eval scorers, and an ADK-pattern pass
across the example agents (safety posture, tool design, SQL identifiers).

### Added

- **Dev UI workspace discovery (#594).** Discover workspace agents, tools, and
  APIs from Apps; new discover surfaces + models for browsing live inventory.
- **Topology wire/chat + OKF generate-pack (#595).** Wire/chat flows in the
  topology UI, OKF pack generation, and samples DataAgent fleet support.
- **Remote long-task continuation (#604).** Remote agents can continue across
  long-running turns instead of timing out mid-task.
- **CLEARS-aligned eval scorers (#596).** Safety default + `clears_scorers()`
  helper for CLEARS-style evaluation.
- **Doctor AI Gateway guardrail warning.** `apx-agent doctor` warns when an
  Apps-target model endpoint lacks AI Gateway guardrails.
- **ADK-pattern agent skills (#597).** Three skills covering multi-agent
  composition, tool design, and safety/callbacks.

### Fixed

- **ADK example audit (#598–#608).** SQL identifier validation before unquoted
  interpolation (#599); `customer_triage` `HandoffAgent` → `RouterAgent`
  (#600); tool-shape cleanups + `attach_resources` (#601); per-request memory
  principals, slack OAuth nonce TTL, apx-builder codegen validation +
  `PolicyGate` ASK on scaffold/deploy (#602).
- **uv.lock heal.** Idempotent lock regenerate + frozen session-hook path so
  pypi-proxy churn doesn't poison the tree.

### Docs / chore

- README leads with `LlmAgent` as the product hero (#607).
- ADK audit report (`docs/adk-audit-2026-08-03.md`); ctk-verify agent + loop
  commands for claim-vs-reality checks.

## [0.4.6] — 2026-07-28

- Apps scaffold CI, pin checks, deploy state, destroy/status (#593).

## [0.4.5] — 2026-07-28

Large mid-cycle drop after 0.4.4: OKF↔UC comment sync / enrich / drift-PR CLI,
OBO scope derivation at deploy time, ToolError containment in the runtime and
metadata factories, declarative `vector_search_index` wiring, SQL helper
consolidation onto databricks-tools-core, and the zero-ops-diagnostics +
customer_triage_fleet examples. See the
[v0.4.5 GitHub release](https://github.com/stuagano/apx-agent/releases/tag/v0.4.5)
for the full PR list.

## [0.4.4] — 2026-07-19

Headline: natural-language agent authoring (`apx-agent generate`), a
multi-environment deploy story, and consolidation of the persistent stores
onto Lakebase. Plus a broad CLI-correctness pass — `--format json` /
`--json` across more commands, honest exit codes, corrected command hints,
and swallowed-exception fixes — and the example-apps and scaffold fixes.

**BREAKING:** the Delta-backed memory / conversation / example stores are
removed (#332). `memory="persistent"` now maps to Lakebase; projects that
relied on the Delta backend must move to Lakebase (or `managed`).

### Added

- **`apx-agent generate` (#516).** Natural-language agent authoring — describe
  an agent and get a scaffolded project, with scaffold output standardized
  across templates and the coworker gallery.
- **Multi-environment deploy (#510).** Per-env UC catalog/schema with a
  staging DAB target, so one project deploys cleanly across environments.
- **`apx-agent onboard` (#318).** Guided non-profit onboarding interview.
- **`apx-agent agents redeploy` (#538).** Redeploy from the remembered local
  checkout.
- **hubspot-complaints-agent example (#513).** Summarizes HubSpot complaints
  by month.
- **Wider machine-readable output.** `--format json` for `agents apps` (#547)
  and `fleet tag/backfill/repoint/redeploy` (#544); `--json` for `agents
  delete` (#546); `--json-output` for `uc publish` (#545).

### Changed

- **Persistent stores consolidated on Lakebase (#332, #512).** Delta memory /
  conversation / example stores deleted; `persistent` remaps to Lakebase.
- **Backlog moved back to GitHub issues (#518).** `docs/BACKLOG.md` removed.
- `uc publish` now fails with a non-zero exit code on a registry-write
  failure (#545).
- `run_sql` polls to real completion instead of treating a still-running
  statement as a failure (#537).

### Fixed

- **Scaffold / deploy correctness.** Scaffold builds one `WorkspaceClient`
  across branches (#554/#522); honors an explicit non-TTY `--target`, mounts
  `/readyz`, makes tools optional (#459); aligns the guardrail default and
  standardizes `uc --profile` position (#534); clarifies
  refresh-schema/migrate-to-okf/pull-comments scope (#535).
- **`deploy` no longer destroys `databricks.yml` comments** with
  `--env`/`--secret-env`/`--auto-update-yml` (#536); reuses its own
  `_json_cli_errors` helper (#548); drops a duplicate
  framework-source-injection block (#541).
- **Example Apps deploys resolve cleanly** for memory_demo / customer_triage /
  data-* (#555).
- Swallowed exceptions surfaced in `_ws_list_*` / `_make_ws_for_scaffold`
  (#542); `--coworker NAME` gallery lookup deduped and a stray f-string fixed
  (#540).
- Corrected stale pre-migration command hints and self-references —
  run/deploy/scaffold (#550), agents stop/apps/deploy (#543), moved-command
  references (#551).
- `agents describe` no longer requires resolved `$CATALOG`/`$SCHEMA` (#515).
- `drop_orphaned_tool_outputs` also drops orphaned tool calls (#519).
- Managed memory `get()` distinguishes not-found from real infra errors
  (#506); approval decisions stamped on the served resume path (#469/#509).
- `uc publish`/`topology` leaf-level `--profile` forwarding pinned by tests
  (#553/#529).

### Docs / chore

- README by-hand-vs-declared comparison and positioning (#552).
- `make check` auto-sanitizes uv.lock pypi-proxy poisoning (#514); dropped a
  redundant mcp-server sync-tool async patch (#507).

## [0.4.3] — 2026-07-05

A large release. The headline themes: durable mid-turn human approvals and
short-term memory, a fully-managed memory backend, the OKF grounding
substrate maturing (golden queries, glossary), a much deeper CLI (shell,
self-discovery, deploy hardening), cross-agent (A2A) reliability, and two
focused audits — governance/security and memory/conversation-store
correctness — that closed 22 confirmed issues.

**BREAKING:** conversation and checkpoint keys are now namespaced by the
caller's OBO principal, closing a cross-user session-collision hole on
shared-table Apps. Existing in-flight conversations/checkpoints keyed by a
raw `session_id` are unreachable after upgrading — a one-time reset of
in-progress multi-turn sessions.

### Added

- **Durable mid-turn human approval + short-term memory (#329).** A
  LangGraph checkpointer (Lakebase-backed for durability) is wired into the
  served path — ChatAgent, ResponsesAgent, and A2A all surface an
  `approval_required` pause and resume it, and a pending approval survives a
  process restart or lands on another replica. Short-term memory is on by
  default for served `LlmAgent`s.
- **Managed Agent Memory backend (#322).** `memory="managed"` wires
  Databricks' fully-managed memory store end-to-end (add/get/update/delete/
  list/recall), verified against a live store.
- **OKF grounding matures.** Golden queries parse from the Examples section
  and render as labelled Q→SQL few-shot pairs; a `verified_query` tool runs
  them by question match; a glossary/synonyms layer parses from the dataset
  doc and reaches the prompt.
- **CLI: shell + self-discovery.** `apx-agent shell` is an interactive REPL
  (bare `apx-agent` drops into it in a terminal); `describe`/`status` gain
  `--json` and interactive picklists for deployed apps; `agents register`
  backfills a single-agent UC manifest; `agents delete --purge` cascades
  experiments, canary apps, and bundle files.
- **Deploy hardening.** Git/lock provenance is stamped on every deploy and
  checked for drift by `doctor`; `--dry-run`, `--env`/`--secret-env`,
  `--timeout`/`--readyz-retries`; model-serving deploy gates on endpoint
  READY + a smoke invocation; `agents status` reports post-deploy health and
  provenance in one command.
- **Dev-UI un-hidden.** Setup-discovery, trace/approval, and codegen routes
  are typed and validated instead of hidden JSON blobs; field-description
  curation gets an LLM-suggestion panel.
- **A2A / multi-agent.** Trace correlation (traceparent + caller identity)
  crosses the agent boundary; Apps-hosted agents register as app-type
  supervisor tools; sub-agent reachability surfaces in `doctor`, `agents
  status`, and `/readyz`; remote tool schemas propagate from the discovery
  card instead of collapsing to `{message}`.

### Fixed — governance & security audit (#463–481)

- The raw `/tools/<name>` + MCP path no longer silently falls back to the app
  service principal on a missing OBO token (confused deputy) — it now fails
  closed like the compiled serving path, and drops the App's own hostname
  from the OBO client instead of hanging.
- Agent/tool registry writes now require ownership — closes a spoofing hole
  where any writer could repoint or unregister another user's published
  agent.
- `executor="claude-sdk"` no longer bypasses configured approval, watchdog,
  and rate-limit guards; it now falls back to the governed LangGraph path
  when any are configured.
- A2A no longer launders privilege when a request carries a `user_token` but
  no `user_id` (a normal Model Serving shape) — the sub-agent hop now
  forwards the token it should.
- `forget` no longer deletes another principal's memory by id.
- Dev-UI per-principal reads (approvals, conversations, memories, traces)
  now require the operator token on a deployed App, same as writes.
- Approval decisions record who decided and when; traces record a
  `user_hash` so an action is attributable to a user, not just "a token was
  present."

### Fixed — memory & conversation-store audit (#482–493)

- A composite agent (`SequentialAgent`/`LoopAgent`/...) with a Lakebase
  session no longer crashes every turn with a 500.
- `InMemoryMemoryStore`/`InMemoryExampleStore` are now thread-safe — a
  concurrent write no longer crashes an in-flight `list`/`recall` scan.
- Chat streaming no longer double-writes the user prompt on an approval
  resume (a gap in the earlier #375 fix).
- Lakebase `update()` no longer resurrects a concurrently-deleted memory;
  `get`/`delete` now propagate real infra errors instead of reporting a
  false "not found."
- The managed memory store's `list`/`recall` now page through the full
  result set instead of silently truncating at one REST page.
- Conversation history replay now reads the most recent turns, not the
  oldest 10k — a long conversation no longer freezes on ancient history or
  bricks on a boundary-split tool call/result pair.
- A transient history-load failure no longer drops the whole turn from the
  conversation.
- `/readyz` now reports a degraded durable checkpointer instead of silently
  running in-process memory.
- An assistant turn's prose is preserved alongside its tool calls on the
  chat path (it used to be dropped from the stored transcript).

### Fixed — everything else (selected)

- SQL string-literal escaping consolidated onto one canonical, correctly
  backslash-escaping implementation (closed an injection path).
- Watchdog governance fails **closed** by default when the gate is
  unreachable.
- Lakebase conversation appends are serialized (no more duplicate
  positions); sibling Lakebase engines and the checkpointer pool are
  disposed on shutdown; ILIKE search escapes wildcard metacharacters.
- A2A resume errors stay inside the JSON-RPC contract instead of surfacing
  as a raw HTTP 500.
- `uv.lock` proxy-URL sanitization restores state on a failed deploy instead
  of leaving it mutated.
- `/invocations` accepts both ChatAgent message shapes; the remote client
  parses both reply shapes.

## [0.4.2] — 2026-06-23

`agents list` sees Databricks Apps; root chat matches the dev UI.

### Added

- **`agents list` discovers Apps-deployed agents.** It used to show only
  UC-registered models, so agents deployed to the Apps target without UC
  registration were invisible. It now also probes each Databricks App's
  `/.well-known/agent.json` (A2A card) using the SDK's own credentials, and
  merges the results into one table with a **SERVING** column
  (`model-serving` / `apps`). An apps-deploy that also has a UC manifest is
  deduped into one row carrying both the UC name and the live URL.
- **`agents list --apps-only`** — skip the UC scan and show just the
  Apps-deployed agents.

### Changed

- The root chat at `/` now uses the apx-agent dev-UI dark palette (same
  background, panels, and accent) for a consistent look between the end-user
  chat and the dev console.

## [0.4.1] — 2026-06-23

End-user chat at the agent's root URL.

### Added

- **Chat UI at `/`.** A deployed agent now serves a self-contained end-user
  chat at its root URL, separate from the developer console under `/_apx/*`
  (which stays off in Apps). The page talks to the live `/responses` contract,
  inlines its markdown renderer (no CDN, private-link-safe), and titles itself
  from the served agent's `name` + `description`.

### Fixed

- The root chat targets `/responses` (the ResponsesAgent `{input}`→`{output}`
  contract served identically on every path), not `/invocations` — which is
  ChatAgent `{messages}` under `create_app` but ResponsesAgent under Apps, so it
  400'd on real Apps deploys.
- `hello-world` and `payroll-coworker` shipped a stale `start_server.py` that
  imported the renamed `resolve_session_store` — crashing on startup when
  deployed from `main`. Renamed to `resolve_conversation_store`.

## [0.4.0] — 2026-06-23

A large release. The headline themes: one canonical Python definition per
agent, governance that's actually enforced on the served path, keyed shared
state between sub-agents, the OKF grounding substrate, and a judge-alignment
labeling loop.

### Added

- **One Python definition per agent.** `apx-agent deploy` on a YAML spec now
  codegens `agent.py`, and the Edit page renders that same definition — the
  Python file is the single source of truth, no hidden divergence between
  declared and served behavior.
- **Governance enforced on the served path (G1/G2).** Guardrails and agent
  callbacks now run on the served request, not just locally. The Apps runtime
  fails closed when OBO identity is missing, and warns when a request would
  fall open to the app service principal or when guardrails/callbacks can't be
  enforced.
- **Keyed shared state (G3).** `output_key` + `{key}` templating pass values
  between sub-agents; `Dependencies.State` exposes tool-level keyed state; and
  state persists across a session (session-scoped persistence, optional durable
  Lakebase session store enabled by default in new scaffolds).
- **OKF grounding substrate.** Open-format `.apx/okf/` bundle is the grounding
  source of truth (schema.json becomes a derived cache): enrichment reaches the
  prompt, bundle lifecycle (scaffold / refresh / cache hook), a `knowledge=`
  envelope declaration, and read-only `pull-comments` (UC → OKF). UC writes go
  through a governed `uc_comment_writer` tool (grants + OBO + audit), never a
  raw CLI `--apply`.
- **Judge-alignment labeling.** `apx-agent label` runs an MLflow
  judge-alignment loop (start/align), works end-to-end on Databricks, and is
  usable against Apps-deployed agents.
- **Fleet operations.** `apx-agent fleet` runs workspace-scoped bulk operations
  (list / tag / backfill / redeploy) across many agents from a shared selector.
- **Onboarding & CLI.** `describe` reads a YAML spec (closes the
  scaffold→describe loop); `status` + run-from-spec; `doctor` resolves an
  ambiguous Databricks profile interactively; scaffold guides Lakebase session
  setup.
- **Pre-grounded DataAgent.** Deploy/run bakes `.apx/schema.json` so a DataAgent
  knows its tables and columns without a `SHOW TABLES` round-trip.

### Fixed

- Deploy no longer breaks on the hatch-vcs dynamic version (`--target apps`).
- Compiled `SequentialAgent` inserts a continuation user turn between steps;
  `LoopAgent` forwards its timeout.
- Proxy package-download URLs are sanitized out of `uv.lock` (not just the
  index) on deploy, keeping releases on public PyPI.
- Five confirmed correctness bugs from the ADK gap audit.
- Trace reads migrated off the deprecated `mlflow.search_traces` arguments
  (`experiment_names` / `experiment_ids`) for MLflow 3.x.
- MCP and dev-UI mount failures surface via `/readyz` instead of being
  swallowed; MCP setup import failures are distinguished.
- Dev UI: eval and read-only GET routes are typed, validated, and un-hidden.

### Changed

- Pyright type-debt registry paid down module-by-module (cli, catalog,
  topology, and others) — the exclude list masked real bugs; several were fixed
  in the process.
- CI runs tests in parallel (`pytest-xdist -n auto`), cancels superseded PR
  runs, caches uv deps, and lets docs-only PRs satisfy required checks.
- Shared SQL/memory helpers deduplicated (`_sql.sql_str_literal`,
  `_sql.sql_escape`); voynich examples removed.

[0.4.3]: https://github.com/stuagano/apx-agent/releases/tag/v0.4.3
[0.4.2]: https://github.com/stuagano/apx-agent/releases/tag/v0.4.2
[0.4.1]: https://github.com/stuagano/apx-agent/releases/tag/v0.4.1
[0.4.0]: https://github.com/stuagano/apx-agent/releases/tag/v0.4.0
