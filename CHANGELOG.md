# Changelog

All notable changes to apx-agent. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are git tags
(`v*`) and the wheel version is derived from the tag via hatch-vcs.

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
