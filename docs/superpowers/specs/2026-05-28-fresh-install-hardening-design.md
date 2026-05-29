# Fresh-install hardening + `apx doctor` — design

**Date:** 2026-05-28
**Status:** Approved (design); pending implementation plan
**Scope:** End-user adopter journey — `uv add apx-agent` → `apx scaffold` → `uv sync` → edit `agent.py` → `apx run` → `apx deploy`

## Problem

A developer adopting apx-agent into their own project hits confusing failures
during first use: deep SDK tracebacks, missing prerequisites surfacing as
buried uvicorn errors, no single place to see "what's wrong with my setup."
The CLI already has good isolated patterns (`_preflight_databricks_auth`
converts an SDK traceback into one clear line; `_preflight_apps` checks layout;
`_validate_responses_agent_compiler` checks an extra), but coverage is uneven
and there is no consolidated diagnostic.

## Goal

Make the four first-run surfaces — `scaffold`, `run`, `deploy`, and the
top-level entry — fail with clear, actionable messages, and add an `apx doctor`
command that reports everything wrong at once. One source of truth for "what's
wrong and how to fix it," shared between `doctor` and inline preflights so they
never drift.

Non-goals: hardening the repo-contributor path (`git clone` + `uv sync` from
`python/`); unrelated refactoring. By default `apx doctor` makes one live
workspace round-trip to verify auth actually works; `--offline` skips it for a
fast, network-free run.

## Architecture

New module `python/src/apx_agent/_doctor.py`:

```python
class Status(enum.Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"

@dataclass(frozen=True)
class Check:
    name: str           # "Databricks auth"
    status: Status
    detail: str         # what was found
    fix: str | None     # copy-pasteable next step, or None
```

Each check is a function returning a `Check`: `() -> Check` for environment/auth
checks, `(cwd: Path) -> Check` for project checks. A registry groups them:

- `ENVIRONMENT_CHECKS`
- `AUTH_CHECKS`
- `PROJECT_CHECKS` (take `cwd`)

`_doctor.py` owns the facts and fix hints. `cli.py` owns presentation. Existing
helpers (`_databrickscfg_profiles`, `_detect_target`, the `Config()` probe) are
refactored to feed checks rather than duplicating logic. `_preflight_databricks_auth`
is reimplemented on top of the `databricks_auth` check (same guidance text).

No new runtime dependencies: stdlib `difflib`, `shutil.which`, `subprocess`,
plus the already-present `databricks.sdk`.

## Check catalog

### Environment
- `python_version` — `sys.version_info >= (3, 11)` (matches `requires-python`).
  FAIL with upgrade hint when older.
- `apx_install` — `apx-agent` importable + resolved version. INFO/OK.
- `uv` — `shutil.which("uv")`. WARN (not fatal) with install hint.
- `databricks_cli` — `shutil.which("databricks")` + `databricks --version`
  (subprocess, 1.5s timeout, offline). WARN unless deploying.

### Authentication
- `databricks_auth` — construct `databricks.sdk.core.Config()` (no live call).
  On failure: FAIL with the existing first-timer-vs-ambiguous-profile guidance
  derived from `_databrickscfg_profiles()`. Always runs (offline, fast).
- `databricks_workspace` — **online** check, runs by default (skipped with
  `--offline`). Calls `WorkspaceClient().current_user.me()` (live, ~5s
  timeout) to confirm the resolved token actually authenticates against a
  reachable workspace.
  - SKIP when `--offline` is passed.
  - SKIP if `databricks_auth` already FAILed (nothing to live-test).
  - FAIL with targeted guidance keyed off the error: expired/invalid token
    (→ `databricks auth login` again), host unreachable / DNS / TLS
    (→ check the host URL / VPN), or 403/permission (→ surface workspace +
    the authenticated principal so the user can confirm they hit the right
    workspace). Reports the workspace host and resolved user on success.

### Project (each SKIPs cleanly when `cwd` is not a project)
- `project_layout` — `pyproject.toml` with `[tool.apx.agent]` plus
  `agent.py` (model-serving) or `agent_server/` (apps). Absent → guidance to
  run `apx scaffold`.
- `target` — `model-serving` vs `apps` via `_detect_target`. INFO/OK.
- `extras` — required extra importable for the detected target
  (`langgraph` for model-serving compile path; `apps` for the apps compile
  path, reusing the `compile_to_responses_agent` import probe).
- `uvicorn` — importable (needed by `apx run`). WARN/FAIL with install hint.
- `databricks_yml` — `databricks.yml` present (apps deploy needs it).

Status semantics: `OK`/`SKIP` never fail a command; `WARN` is advisory;
`FAIL` blocks the relevant command and makes `doctor` exit non-zero.

## `apx doctor` command

Runs all checks (environment + auth always; project checks against `cwd`),
prints a grouped checklist with `✓ / ⚠ / ✗ / -` glyphs, each FAIL/WARN
followed by an indented `Fix:` line. Footer summarizes counts and points at the
next action.

```
Environment
  ✓ Python 3.12.2
  ✓ uv 0.5.1
  ⚠ Databricks CLI not found — needed for `apx deploy`
      Fix: brew install databricks/tap/databricks
Authentication
  ✗ Databricks auth unresolved — no profiles in ~/.databrickscfg
      Fix: databricks auth login --host https://<workspace>.cloud.databricks.com
Project (./my-agent)
  ✓ apps layout detected
  ✓ apps extra installed

1 failed, 1 warning. Fix the ✗ items, then re-run `apx doctor`.
```

- Exit code non-zero iff any `FAIL` (CI/script usable). WARN does not fail.
- `--json` flag emits the structured checks for machine consumption.
- The live `databricks_workspace` check (one real workspace round-trip) runs
  by default. Pass `--offline` to skip it when you want a fast, network-free
  run (CI, on a plane, etc.); the rest of the checks are unaffected.

## Inline integration

- **`run`**: preflight = `uvicorn` + `project_layout` + existing auth check.
  Add an **in-process pre-import probe**: import the resolved module before
  handing it to uvicorn; catch `ImportError`/`SyntaxError`/`AttributeError`
  originating in the user's `agent.py` and raise a `ClickException` reporting
  file + line + `apx doctor` pointer, instead of a buried uvicorn-subprocess
  traceback. (The probe must not double-import in a way that breaks
  `--reload`; it runs only in the parent process for diagnostics, and the
  message is surfaced before `uvicorn.run`.)
- **`deploy`**: preflight = `databricks_cli` + `databricks_auth` + existing
  `_preflight_apps`/extras. Wrap `bundle deploy` / `apps deploy` subprocess
  failures with a "run `apx doctor`" pointer and the stderr tail.
- **`scaffold`**: keep existing skip/force/redirect behavior. Add agent-name
  validation messaging and a **next-steps footer** after a successful scaffold:
  `cd <name> && uv sync && apx run`.

## Entry-level

Custom `click.Group` subclass set on `main`:
- Unknown command → `difflib.get_close_matches` "did you mean `deploy`?" hint
  before click's default error.
- No args → one-line orientation appended near help:
  "New here? Run `apx doctor`, then `apx scaffold my-agent`."

## Error-message format

Helper `_fix_msg(title, detail, fix)` returns consistent `ClickException` text:

```
<title>
<detail>

Fix:
    <fix>

Run `apx doctor` for a full check.
```

Every hardened error path uses it (or the `Check.fix` field, which feeds the
same template), so inline errors and `doctor` output read identically.

## Testing

`python/tests/test_doctor.py`:
- Unit-test each check via `monkeypatch` (PATH, env vars, `tmp_path` cwd):
  pass and fail branches for `python_version`, `uv`, `databricks_cli`,
  `databricks_auth`, `project_layout`, `extras`, `uvicorn`, `databricks_yml`.
- `databricks_workspace` (online): mock `WorkspaceClient` to assert the
  SKIP-with-`--offline`, SKIP-when-auth-failed, success, and each
  error-class (expired token / unreachable / 403) branch — no real network.
- `apx doctor` integration: exit code (0 vs non-zero), grouped text output,
  `--json` shape, that the live check runs by default, and that `--offline`
  skips it.
- Entry-level: unknown command emits a "did you mean" suggestion.
- `run` pre-import probe: a deliberately-broken `agent.py` produces the
  friendly file+line message, not a raw traceback.

## Files touched

- New: `python/src/apx_agent/_doctor.py`
- New: `python/tests/test_doctor.py`
- Edit: `python/src/apx_agent/cli.py` — `doctor` command, custom `Group`,
  refactor `_preflight_databricks_auth` onto the check, harden `run`/`deploy`/
  `scaffold` preflights and the `_fix_msg` helper.
- Edit: `README.md` / `docs/getting-started.md` — mention `apx doctor` in the
  quick start troubleshooting note.
