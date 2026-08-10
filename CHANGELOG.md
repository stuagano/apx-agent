# Changelog

All notable changes to apx-agent. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are git tags
(`v*`) and the wheel version is derived from the tag via hatch-vcs.

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
