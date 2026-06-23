# Changelog

All notable changes to apx-agent. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are git tags
(`v*`) and the wheel version is derived from the tag via hatch-vcs.

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

[0.4.1]: https://github.com/stuagano/apx-agent/releases/tag/v0.4.1
[0.4.0]: https://github.com/stuagano/apx-agent/releases/tag/v0.4.0
