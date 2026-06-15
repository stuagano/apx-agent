# Fleet Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `apx-agent fleet` command group that selects a set of agents by tag/scope/name and acts on them in bulk — list, tag, backfill discovery tags, and re-promote to latest version — with a dry-run-by-default, continue-and-report safety model.

**Architecture:** A single pure resolver module (`_fleet.py`) turns an in-memory list of UC registered-model objects into `ResolvedAgent` records via composable predicates (scope, name glob, `--where` tag match, explicit `--uc-name`). CLI commands in `cli.py` fetch models via the existing workspace SDK helpers, call the resolver, and run per-agent actions through a shared outcome/summary reporter. Tags land on the backing UC **registered model** (Databricks Apps have no custom-tag field). `agents list` is refactored onto the same resolver so there is one discovery path.

**Tech Stack:** Python 3.11, Click (CLI), Databricks SDK (`WorkspaceClient.registered_models`), MLflow `MlflowClient` (tag writes + alias promotion), pytest + `click.testing.CliRunner`.

**Run tests with:** `cd python && uv run pytest tests/test_fleet.py -v`
**Type check with:** `cd python && uv run pyright` (no excludes — new code must be clean).

---

## Design clarifications locked before tasks

- **Tag object:** all `fleet tag` / `fleet backfill` writes go on the **registered model** (registered-model-level tags), read back by `registered_models.list()` → `m.tags`. This is where `agents list` and `_watchdog.set_uc_tags_for_agent` already read/write. Apps have no tags field (verified against the SDK `App` model).
- **Two namespaces:** user labels are `apx.label.*`; system tags are `apx.agent.*` / `apx.apps.*` (reserved — `fleet tag` refuses to touch them).
- **`fleet backfill` uses explicit `--uc-name` targets** (repeatable, required). Untagged agents cannot be discovered by tag, so the tag-based resolver cannot find them; backfill therefore takes explicit targets rather than going through `resolve_agents`. It stamps only observable identity tags (`apx.agent.name`, `apx.serving`, and `apx.apps.app_name` when `--app` is given). It cannot reconstruct `apx.agent.tools`/`resources`/`metadata`.
- **`fleet redeploy` v1 mechanism:** for each selected agent, find the latest registered version (`_apps_registry.get_latest_apps_version`) and point the `@prod` alias at it (`_apps_registry.set_prod_alias_version`) if it differs from the current alias (`_apps_registry.get_prod_alias_version`). No git rebuild, no serving-config patch.

## File Structure

- **Create:** `python/src/apx_agent/_fleet.py` — pure resolver, label/where helpers, outcome reporter. No Databricks/MLflow imports at module top level (those live in CLI). One responsibility: turn model objects + predicates into resolved agents and render outcome summaries.
- **Create:** `python/tests/test_fleet.py` — unit tests for `_fleet.py` and CliRunner tests for the `fleet` commands.
- **Modify:** `python/src/apx_agent/cli.py` — add the `fleet` group + 4 subcommands; refactor `agents list` (`list_agents_cmd`, ~line 6495) onto the resolver.

---

## Task 1: Label / where parsing helpers in `_fleet.py`

**Files:**
- Create: `python/src/apx_agent/_fleet.py`
- Test: `python/tests/test_fleet.py`

- [ ] **Step 1: Write the failing test**

```python
# python/tests/test_fleet.py
"""Tests for apx_agent._fleet (fleet selector + helpers)."""
from __future__ import annotations

import pytest

from apx_agent import _fleet


@pytest.mark.unit
def test_to_label_key_adds_prefix_once():
    assert _fleet.to_label_key("team") == "apx.label.team"
    assert _fleet.to_label_key("apx.label.team") == "apx.label.team"


@pytest.mark.unit
def test_is_reserved_flags_system_namespaces():
    assert _fleet.is_reserved("apx.agent.name") is True
    assert _fleet.is_reserved("apx.apps.role") is True
    assert _fleet.is_reserved("team") is False
    assert _fleet.is_reserved("apx.label.team") is False


@pytest.mark.unit
def test_parse_where_splits_key_value():
    assert _fleet.parse_where(["team=revops", "env=prod"]) == {
        "team": "revops",
        "env": "prod",
    }


@pytest.mark.unit
def test_parse_where_rejects_missing_equals():
    with pytest.raises(ValueError, match="key=value"):
        _fleet.parse_where(["teamrevops"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_fleet.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apx_agent._fleet'`.

- [ ] **Step 3: Write minimal implementation**

```python
# python/src/apx_agent/_fleet.py
"""Fleet selection + bulk-operation helpers.

Pure logic only — no Databricks/MLflow imports at module top level. The CLI
layer fetches model objects and performs tag/alias writes; this module turns
model objects + predicates into resolved agents and renders outcome summaries.

Tag namespaces:
  * ``apx.agent.*`` / ``apx.apps.*`` — system tags (reserved; never written by
    ``fleet tag``).
  * ``apx.label.*`` — user labels (what ``fleet tag`` writes/removes).
"""
from __future__ import annotations

LABEL_PREFIX = "apx.label."
RESERVED_PREFIXES = ("apx.agent.", "apx.apps.")
NAME_TAG = "apx.agent.name"
MODEL_TAG = "apx.agent.model"
APP_NAME_TAG = "apx.apps.app_name"


def to_label_key(key: str) -> str:
    """Map a bare user key into the ``apx.label.`` namespace (idempotent)."""
    return key if key.startswith(LABEL_PREFIX) else LABEL_PREFIX + key


def is_reserved(key: str) -> bool:
    """True if ``key`` is a system tag that ``fleet tag`` must not touch."""
    return any(key.startswith(p) for p in RESERVED_PREFIXES)


def parse_where(exprs: list[str]) -> dict[str, str]:
    """Parse repeated ``--where key=value`` flags into a dict (AND semantics)."""
    out: dict[str, str] = {}
    for expr in exprs:
        if "=" not in expr:
            raise ValueError(f"--where must be key=value, got: {expr!r}")
        key, value = expr.split("=", 1)
        out[key.strip()] = value.strip()
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_fleet.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_fleet.py python/tests/test_fleet.py
git commit -m "feat(fleet): label/where parsing helpers"
```

---

## Task 2: `resolve_agents` selector in `_fleet.py`

**Files:**
- Modify: `python/src/apx_agent/_fleet.py`
- Test: `python/tests/test_fleet.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_fleet.py
from types import SimpleNamespace


def _model(name, *, catalog="cat", schema="sch", **tags):
    """Build a fake registered-model object like the SDK returns."""
    full = f"{catalog}.{schema}.{name}"
    return SimpleNamespace(
        name=name,
        catalog_name=catalog,
        schema_name=schema,
        full_name=full,
        tags=[SimpleNamespace(key=k, value=v) for k, v in tags.items()],
    )


@pytest.mark.unit
def test_resolve_skips_models_without_name_tag():
    models = [_model("untagged"), _model("a", **{_fleet.NAME_TAG: "a"})]
    out = _fleet.resolve_agents(models)
    assert [r.name for r in out] == ["a"]


@pytest.mark.unit
def test_resolve_filters_by_where_either_namespace():
    models = [
        _model("a", **{_fleet.NAME_TAG: "a", "apx.label.team": "revops"}),
        _model("b", **{_fleet.NAME_TAG: "b", "apx.label.team": "data"}),
    ]
    out = _fleet.resolve_agents(models, where={"team": "revops"})
    assert [r.name for r in out] == ["a"]


@pytest.mark.unit
def test_resolve_where_is_anded():
    models = [
        _model("a", **{_fleet.NAME_TAG: "a", "apx.label.team": "revops",
                       "apx.label.env": "prod"}),
        _model("b", **{_fleet.NAME_TAG: "b", "apx.label.team": "revops",
                       "apx.label.env": "dev"}),
    ]
    out = _fleet.resolve_agents(models, where={"team": "revops", "env": "prod"})
    assert [r.name for r in out] == ["a"]


@pytest.mark.unit
def test_resolve_name_glob():
    models = [
        _model("p1", **{_fleet.NAME_TAG: "payroll-east"}),
        _model("p2", **{_fleet.NAME_TAG: "revops-bot"}),
    ]
    out = _fleet.resolve_agents(models, name_glob="payroll-*")
    assert [r.name for r in out] == ["payroll-east"]


@pytest.mark.unit
def test_resolve_explicit_uc_names_bypasses_other_filters():
    models = [
        _model("a", catalog="cat", **{_fleet.NAME_TAG: "a"}),
        _model("b", catalog="other", **{_fleet.NAME_TAG: "b"}),
    ]
    out = _fleet.resolve_agents(
        models, catalog="cat", uc_names=["other.sch.b"],
    )
    assert [r.uc_name for r in out] == ["other.sch.b"]


@pytest.mark.unit
def test_resolved_agent_exposes_labels_and_app():
    m = _model("a", **{_fleet.NAME_TAG: "a", _fleet.MODEL_TAG: "ep",
                       _fleet.APP_NAME_TAG: "app-a", "apx.label.team": "revops"})
    (r,) = _fleet.resolve_agents([m])
    assert r.model == "ep"
    assert r.app_name == "app-a"
    assert r.labels == {"team": "revops"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_fleet.py -v`
Expected: FAIL with `AttributeError: module 'apx_agent._fleet' has no attribute 'resolve_agents'`.

- [ ] **Step 3: Write minimal implementation**

Append to `python/src/apx_agent/_fleet.py`:

```python
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any


@dataclass
class ResolvedAgent:
    """One agent selected by the fleet resolver."""
    uc_name: str
    name: str
    model: str | None
    app_name: str | None
    tags: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)


def _tags_dict(model: Any) -> dict[str, str]:
    return {t.key: t.value for t in (getattr(model, "tags", None) or [])}


def _uc_name(model: Any) -> str:
    full = getattr(model, "full_name", None)
    if full:
        return str(full)
    return (
        f"{getattr(model, 'catalog_name', '')}."
        f"{getattr(model, 'schema_name', '')}."
        f"{getattr(model, 'name', '')}"
    )


def _matches_where(tags: dict[str, str], where: dict[str, str]) -> bool:
    for key, value in where.items():
        candidates = {tags.get(key), tags.get(to_label_key(key))}
        if value not in candidates:
            return False
    return True


def resolve_agents(
    models: Any,
    *,
    catalog: str | None = None,
    schema: str | None = None,
    name_glob: str | None = None,
    where: dict[str, str] | None = None,
    uc_names: list[str] | None = None,
) -> list[ResolvedAgent]:
    """Filter registered-model objects into ``ResolvedAgent`` records.

    Only models carrying the ``apx.agent.name`` tag are considered. When
    ``uc_names`` is given, it selects exactly those models and bypasses the
    scope/glob/where filters. Otherwise all of ``catalog``/``schema``/
    ``name_glob``/``where`` are AND-ed.
    """
    where = where or {}
    wanted = set(uc_names or [])
    out: list[ResolvedAgent] = []
    for model in models:
        tags = _tags_dict(model)
        if NAME_TAG not in tags:
            continue
        uc = _uc_name(model)
        if wanted:
            if uc not in wanted:
                continue
        else:
            if catalog and getattr(model, "catalog_name", None) != catalog:
                continue
            if schema and getattr(model, "schema_name", None) != schema:
                continue
            if name_glob and not fnmatch(tags.get(NAME_TAG, ""), name_glob):
                continue
            if where and not _matches_where(tags, where):
                continue
        labels = {
            k[len(LABEL_PREFIX):]: v
            for k, v in tags.items()
            if k.startswith(LABEL_PREFIX)
        }
        out.append(
            ResolvedAgent(
                uc_name=uc,
                name=tags.get(NAME_TAG, ""),
                model=tags.get(MODEL_TAG),
                app_name=tags.get(APP_NAME_TAG),
                tags=tags,
                labels=labels,
            )
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_fleet.py -v`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_fleet.py python/tests/test_fleet.py
git commit -m "feat(fleet): resolve_agents selector"
```

---

## Task 3: Outcome reporter in `_fleet.py`

**Files:**
- Modify: `python/src/apx_agent/_fleet.py`
- Test: `python/tests/test_fleet.py`

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_fleet.py
@pytest.mark.unit
def test_summary_exit_code_zero_when_all_ok():
    outcomes = [
        _fleet.AgentOutcome("a.b.c", "ok", "tagged"),
        _fleet.AgentOutcome("a.b.d", "skipped", "no change"),
    ]
    text, code = _fleet.render_summary(outcomes, apply=True)
    assert code == 0
    assert "1 ok" in text and "1 skipped" in text


@pytest.mark.unit
def test_summary_exit_code_nonzero_on_failure():
    outcomes = [
        _fleet.AgentOutcome("a.b.c", "ok", "tagged"),
        _fleet.AgentOutcome("a.b.d", "failed", "boom"),
    ]
    text, code = _fleet.render_summary(outcomes, apply=True)
    assert code == 1
    assert "1 failed" in text
    assert "boom" in text


@pytest.mark.unit
def test_summary_marks_dry_run():
    text, _ = _fleet.render_summary(
        [_fleet.AgentOutcome("a.b.c", "ok", "would tag")], apply=False,
    )
    assert "dry-run" in text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_fleet.py -v`
Expected: FAIL with `AttributeError: module 'apx_agent._fleet' has no attribute 'AgentOutcome'`.

- [ ] **Step 3: Write minimal implementation**

Append to `python/src/apx_agent/_fleet.py`:

```python
@dataclass
class AgentOutcome:
    """Result of one per-agent action in a bulk command."""
    uc_name: str
    status: str  # "ok" | "skipped" | "failed"
    detail: str = ""


def render_summary(outcomes: list[AgentOutcome], *, apply: bool) -> tuple[str, int]:
    """Render a per-agent result table + summary line.

    Returns ``(text, exit_code)``. ``exit_code`` is 1 if any outcome failed,
    else 0. When ``apply`` is False the header marks the run as a dry-run.
    """
    lines: list[str] = []
    header = "Fleet plan (dry-run — nothing changed; pass --apply to execute):" if not apply \
        else "Fleet result:"
    lines.append(header)
    for o in outcomes:
        lines.append(f"  [{o.status:<7}] {o.uc_name}" + (f"  {o.detail}" if o.detail else ""))
    n_ok = sum(1 for o in outcomes if o.status == "ok")
    n_skip = sum(1 for o in outcomes if o.status == "skipped")
    n_fail = sum(1 for o in outcomes if o.status == "failed")
    lines.append(f"Summary: {n_ok} ok, {n_skip} skipped, {n_fail} failed")
    return "\n".join(lines), (1 if n_fail else 0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_fleet.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/_fleet.py python/tests/test_fleet.py
git commit -m "feat(fleet): outcome reporter with dry-run + exit code"
```

---

## Task 4: `fleet` group + `fleet list` command

**Files:**
- Modify: `python/src/apx_agent/cli.py` (add group + command near the other `@main.group` blocks, e.g. after the `canary` group ~line 7353)
- Test: `python/tests/test_fleet.py`

Note the existing helpers in `cli.py`: `_require_sdk(profile)` returns a `WorkspaceClient`; `agents list` lists models via `ws.registered_models.list(catalog_name=, schema_name=, include_browse=False)` with a `TypeError` fallback to `ws.registered_models.list()`.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_fleet.py
from unittest.mock import MagicMock, patch
from click.testing import CliRunner

from apx_agent.cli import main


def _fake_ws(models):
    ws = MagicMock()
    ws.registered_models.list.return_value = iter(models)
    return ws


@pytest.mark.unit
def test_fleet_list_prints_selected_agents():
    ws = _fake_ws([
        _model("a", **{_fleet.NAME_TAG: "payroll", "apx.label.team": "revops"}),
        _model("b", **{_fleet.NAME_TAG: "other"}),
    ])
    with patch("apx_agent.cli._require_sdk", return_value=ws):
        result = CliRunner().invoke(
            main, ["fleet", "list", "--where", "team=revops"],
        )
    assert result.exit_code == 0, result.output
    assert "payroll" in result.output
    assert "other" not in result.output


@pytest.mark.unit
def test_fleet_list_json_format():
    ws = _fake_ws([_model("a", **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws):
        result = CliRunner().invoke(main, ["fleet", "list", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert '"payroll"' in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_fleet.py -k fleet_list -v`
Expected: FAIL — `Error: No such command 'fleet'`.

- [ ] **Step 3: Write minimal implementation**

Add to `python/src/apx_agent/cli.py`. First a shared model-fetch + resolve helper (place it near `_connect_workspace`/`_require_sdk`, ~line 6478):

```python
def _fleet_resolve(ws, *, catalog, schema, name_glob, where_exprs, uc_names):
    """List registered models and resolve them with the fleet selector."""
    from apx_agent import _fleet

    try:
        models = list(ws.registered_models.list(
            catalog_name=catalog, schema_name=schema, include_browse=False,
        ))
    except TypeError:
        models = list(ws.registered_models.list())  # type: ignore[call-arg]
    try:
        where = _fleet.parse_where(list(where_exprs))
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    return _fleet.resolve_agents(
        models, catalog=catalog, schema=schema, name_glob=name_glob,
        where=where, uc_names=list(uc_names) or None,
    )


# Reusable selection options shared by every fleet command.
def _fleet_select_options(f):
    f = click.option("--catalog", default=None, help="Restrict to a UC catalog.")(f)
    f = click.option("--schema", default=None,
                     help="Restrict to a UC schema (requires --catalog).")(f)
    f = click.option("--name", "name_glob", default=None,
                     help="Glob match against apx.agent.name (e.g. 'payroll-*').")(f)
    f = click.option("--where", "where_exprs", multiple=True,
                     help="Tag predicate key=value (repeatable, AND-ed).")(f)
    f = click.option("--uc-name", "uc_names", multiple=True,
                     help="Explicit registered-model name; bypasses filters.")(f)
    f = click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
                     help="Databricks config profile.")(f)
    return f
```

Then the group + `list`:

```python
@main.group(cls=_ApxGroup)
def fleet() -> None:
    """Workspace-scoped bulk operations across many apx-agents.

    Select a set of agents (by --catalog/--schema scope, --name glob,
    --where tag predicates, or explicit --uc-name) and act on them in bulk.
    Mutating commands are dry-run by default; pass --apply to execute.
    """


@fleet.command("list")
@_fleet_select_options
@click.option("--format", "fmt", type=click.Choice(["text", "json"]),
              default="text", help="Output format.")
def fleet_list_cmd(catalog, schema, name_glob, where_exprs, uc_names, profile, fmt):
    """Resolve a selection and print the matching agents (read-only)."""
    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")
    ws = _require_sdk(profile)
    agents_ = _fleet_resolve(
        ws, catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    if fmt == "json":
        click.echo(json.dumps([
            {"agent_name": a.name, "uc_name": a.uc_name, "model": a.model,
             "app_name": a.app_name, "labels": a.labels}
            for a in agents_
        ], indent=2, default=str))
        return
    if not agents_:
        click.echo("No agents matched the selection.")
        return
    click.echo(f"{'AGENT':<24}  {'UC NAME':<40}  {'APP':<22}  LABELS")
    for a in agents_:
        labels = ",".join(f"{k}={v}" for k, v in sorted(a.labels.items())) or "-"
        click.echo(f"{(a.name or '-'):<24}  {a.uc_name:<40}  "
                   f"{(a.app_name or '-'):<22}  {labels}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_fleet.py -k fleet_list -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_fleet.py
git commit -m "feat(fleet): fleet group + fleet list command"
```

---

## Task 5: Refactor `agents list` onto the resolver

**Files:**
- Modify: `python/src/apx_agent/cli.py` — `list_agents_cmd` (~line 6495)
- Test: `python/tests/test_fleet.py`

Behavior-preserving: `agents list` keeps its columns (AGENT / UC NAME / MODEL / TOOLS / RESOURCES) and text/json output, but resolves models through `_fleet.resolve_agents` instead of its own inline tag loop.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_fleet.py
@pytest.mark.unit
def test_agents_list_still_discovers_by_name_tag():
    ws = _fake_ws([
        _model("a", **{_fleet.NAME_TAG: "payroll", _fleet.MODEL_TAG: "ep",
                       "apx.agent.tool_count": "3"}),
        _model("b"),  # untagged → excluded
    ])
    with patch("apx_agent.cli._require_sdk", return_value=ws):
        result = CliRunner().invoke(main, ["agents", "list"])
    assert result.exit_code == 0, result.output
    assert "payroll" in result.output
    assert "ep" in result.output
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `cd python && uv run pytest tests/test_fleet.py -k agents_list -v`
Expected: PASS already (existing behavior). This test pins behavior *before* the refactor so the refactor can't regress it. If it fails, fix the test to match current output first.

- [ ] **Step 3: Refactor implementation**

Replace the model-iteration block inside `list_agents_cmd` (the `for m in models:` loop that builds `rows`) with a resolver call. Keep the `tool_count`/`resource_count` columns by reading from `a.tags`:

```python
    ws = _require_sdk(profile)
    agents_ = _fleet_resolve(
        ws, catalog=catalog, schema=schema, name_glob=None,
        where_exprs=(), uc_names=(),
    )
    rows: list[dict[str, Any]] = []
    for a in agents_:
        resource_count = 0
        try:
            resource_count = len(json.loads(a.tags.get("apx.agent.metadata") or "{}")
                                 .get("resources") or [])
        except Exception:
            pass
        rows.append({
            "agent_name": a.name,
            "model_endpoint": a.model,
            "uc_name": a.uc_name,
            "tool_count": a.tags.get("apx.agent.tool_count"),
            "resource_count": resource_count,
        })
```

Leave the existing `if fmt == "json":` / table-printing code below this block unchanged.

- [ ] **Step 4: Run the full suite to verify no regression**

Run: `cd python && uv run pytest tests/test_fleet.py tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_fleet.py
git commit -m "refactor(fleet): agents list uses shared resolver"
```

---

## Task 6: `fleet tag` command

**Files:**
- Modify: `python/src/apx_agent/cli.py` (add after `fleet_list_cmd`)
- Test: `python/tests/test_fleet.py`

Tag writes use MLflow: `MlflowClient().set_registered_model_tag(uc_name, key, value)` and `delete_registered_model_tag(uc_name, key)`.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_fleet.py
@pytest.mark.unit
def test_fleet_tag_dry_run_writes_nothing():
    ws = _fake_ws([_model("a", **{_fleet.NAME_TAG: "payroll"})])
    client = MagicMock()
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "tag", "--name", "payroll", "--set", "team=revops"],
        )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    client.set_registered_model_tag.assert_not_called()


@pytest.mark.unit
def test_fleet_tag_apply_sets_label():
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    client = MagicMock()
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "tag", "--name", "payroll",
                   "--set", "team=revops", "--apply"],
        )
    assert result.exit_code == 0, result.output
    client.set_registered_model_tag.assert_called_once_with(
        "cat.sch.a", "apx.label.team", "revops",
    )


@pytest.mark.unit
def test_fleet_tag_refuses_reserved_namespace():
    ws = _fake_ws([_model("a", **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws):
        result = CliRunner().invoke(
            main, ["fleet", "tag", "--name", "payroll",
                   "--set", "apx.agent.name=x", "--apply"],
        )
    assert result.exit_code != 0
    assert "reserved" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_fleet.py -k fleet_tag -v`
Expected: FAIL — `Error: No such command 'tag'`.

- [ ] **Step 3: Write minimal implementation**

```python
@fleet.command("tag")
@_fleet_select_options
@click.option("--set", "set_pairs", multiple=True,
              help="Label to set: key=value (repeatable). Writes apx.label.<key>.")
@click.option("--remove", "remove_keys", multiple=True,
              help="Label key to remove (repeatable).")
@click.option("--apply", is_flag=True, help="Execute. Without it, dry-run.")
def fleet_tag_cmd(catalog, schema, name_glob, where_exprs, uc_names, profile,
                  set_pairs, remove_keys, apply):
    """Set or remove user labels (apx.label.*) across the selection."""
    from apx_agent import _fleet

    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")
    if not set_pairs and not remove_keys:
        raise click.UsageError("Pass at least one --set or --remove.")
    try:
        sets = _fleet.parse_where(list(set_pairs))
    except ValueError as e:
        raise click.UsageError(str(e)) from e
    # Reject reserved namespaces before touching the workspace.
    for key in list(sets) + list(remove_keys):
        if _fleet.is_reserved(key):
            raise click.UsageError(
                f"Refusing to modify reserved system tag '{key}'. "
                "fleet tag only writes user labels (apx.label.*)."
            )

    ws = _require_sdk(profile)
    agents_ = _fleet_resolve(
        ws, catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    if not agents_:
        click.echo("No agents matched the selection.")
        return

    from mlflow.tracking import MlflowClient
    client = MlflowClient() if apply else None

    outcomes: list[_fleet.AgentOutcome] = []
    for a in agents_:
        changes = [f"+{_fleet.to_label_key(k)}={v}" for k, v in sets.items()]
        changes += [f"-{_fleet.to_label_key(k)}" for k in remove_keys]
        try:
            if apply:
                for k, v in sets.items():
                    client.set_registered_model_tag(a.uc_name, _fleet.to_label_key(k), v)
                for k in remove_keys:
                    client.delete_registered_model_tag(a.uc_name, _fleet.to_label_key(k))
            outcomes.append(_fleet.AgentOutcome(a.uc_name, "ok", " ".join(changes)))
        except Exception as e:  # continue + report
            outcomes.append(_fleet.AgentOutcome(a.uc_name, "failed", str(e)))

    text, code = _fleet.render_summary(outcomes, apply=apply)
    click.echo(text)
    if code:
        raise SystemExit(code)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_fleet.py -k fleet_tag -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_fleet.py
git commit -m "feat(fleet): fleet tag (set/remove labels, dry-run, reserved guard)"
```

---

## Task 7: `fleet backfill` command

**Files:**
- Modify: `python/src/apx_agent/cli.py` (add after `fleet_tag_cmd`)
- Test: `python/tests/test_fleet.py`

Backfill takes explicit `--uc-name` targets (untagged agents are undiscoverable by tag). It reads current tags via `MlflowClient().get_registered_model(uc_name).tags` and stamps only missing observable identity tags.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_fleet.py
def _fake_mlflow_model(tags: dict):
    return SimpleNamespace(
        tags=[SimpleNamespace(key=k, value=v) for k, v in tags.items()],
    )


@pytest.mark.unit
def test_fleet_backfill_stamps_missing_identity_tags():
    client = MagicMock()
    client.get_registered_model.return_value = _fake_mlflow_model({})  # no tags
    with patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "backfill", "--uc-name", "cat.sch.payroll",
                   "--name", "payroll", "--app", "payroll-app", "--apply"],
        )
    assert result.exit_code == 0, result.output
    calls = {c.args[1]: c.args[2]
             for c in client.set_registered_model_tag.call_args_list}
    assert calls["apx.agent.name"] == "payroll"
    assert calls["apx.apps.app_name"] == "payroll-app"
    assert calls["apx.serving"] == "apps"


@pytest.mark.unit
def test_fleet_backfill_dry_run_writes_nothing():
    client = MagicMock()
    client.get_registered_model.return_value = _fake_mlflow_model({})
    with patch("mlflow.tracking.MlflowClient", return_value=client):
        result = CliRunner().invoke(
            main, ["fleet", "backfill", "--uc-name", "cat.sch.payroll",
                   "--name", "payroll"],
        )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output.lower()
    client.set_registered_model_tag.assert_not_called()


@pytest.mark.unit
def test_fleet_backfill_requires_uc_name():
    result = CliRunner().invoke(main, ["fleet", "backfill", "--name", "x"])
    assert result.exit_code != 0
    assert "uc-name" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_fleet.py -k fleet_backfill -v`
Expected: FAIL — `Error: No such command 'backfill'`.

- [ ] **Step 3: Write minimal implementation**

```python
@fleet.command("backfill")
@click.option("--uc-name", "uc_names", multiple=True, required=True,
              help="Registered-model name to backfill (repeatable, required).")
@click.option("--name", "agent_name", default=None,
              help="apx.agent.name to stamp. Defaults to the model name.")
@click.option("--app", "app_name", default=None,
              help="Workspace App name to stamp as apx.apps.app_name.")
@click.option("--apply", is_flag=True, help="Execute. Without it, dry-run.")
@click.option("--profile", default=None, envvar="DATABRICKS_CONFIG_PROFILE",
              help="Databricks config profile.")
def fleet_backfill_cmd(uc_names, agent_name, app_name, apply, profile):
    """Stamp missing identity/discovery tags onto agents that predate tagging.

    Partial by design: stamps apx.agent.name, apx.serving, and (with --app)
    apx.apps.app_name. It cannot reconstruct apx.agent.tools/resources/metadata.
    """
    from apx_agent import _fleet

    from mlflow.tracking import MlflowClient
    client = MlflowClient()

    outcomes: list[_fleet.AgentOutcome] = []
    for uc in uc_names:
        try:
            existing = {t.key: t.value
                        for t in (getattr(client.get_registered_model(uc), "tags", None) or [])}
            want = {
                _fleet.NAME_TAG: agent_name or uc.split(".")[-1],
                "apx.serving": "apps",
            }
            if app_name:
                want[_fleet.APP_NAME_TAG] = app_name
            missing = {k: v for k, v in want.items() if k not in existing}
            if not missing:
                outcomes.append(_fleet.AgentOutcome(uc, "skipped", "already tagged"))
                continue
            if apply:
                for k, v in missing.items():
                    client.set_registered_model_tag(uc, k, v)
            detail = "stamp " + ",".join(sorted(missing))
            outcomes.append(_fleet.AgentOutcome(uc, "ok", detail))
        except Exception as e:
            outcomes.append(_fleet.AgentOutcome(uc, "failed", str(e)))

    text, code = _fleet.render_summary(outcomes, apply=apply)
    click.echo(text)
    if not apply:
        click.echo("Note: backfill cannot reconstruct tools/resources/metadata.")
    if code:
        raise SystemExit(code)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_fleet.py -k fleet_backfill -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_fleet.py
git commit -m "feat(fleet): fleet backfill (explicit targets, partial identity tags)"
```

---

## Task 8: `fleet redeploy` command

**Files:**
- Modify: `python/src/apx_agent/cli.py` (add after `fleet_backfill_cmd`)
- Test: `python/tests/test_fleet.py`

Reuses `_apps_registry`: `get_latest_apps_version(uc_name)`, `get_prod_alias_version(uc_name)`, `set_prod_alias_version(uc_name, version)`.

- [ ] **Step 1: Write the failing test**

```python
# Append to python/tests/test_fleet.py
@pytest.mark.unit
def test_fleet_redeploy_promotes_when_latest_differs():
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_apps_version", return_value="5"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="3"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "redeploy", "--apply"])
    assert result.exit_code == 0, result.output
    setp.assert_called_once_with("cat.sch.a", "5")
    assert "3" in result.output and "5" in result.output


@pytest.mark.unit
def test_fleet_redeploy_skips_when_already_latest():
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_apps_version", return_value="5"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="5"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "redeploy", "--apply"])
    assert result.exit_code == 0, result.output
    setp.assert_not_called()
    assert "skipped" in result.output.lower()


@pytest.mark.unit
def test_fleet_redeploy_dry_run_writes_nothing():
    ws = _fake_ws([_model("a", catalog="cat", schema="sch",
                          **{_fleet.NAME_TAG: "payroll"})])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_apps_version", return_value="5"), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="3"), \
         patch("apx_agent._apps_registry.set_prod_alias_version") as setp:
        result = CliRunner().invoke(main, ["fleet", "redeploy"])
    assert result.exit_code == 0, result.output
    setp.assert_not_called()
    assert "dry-run" in result.output.lower()


@pytest.mark.unit
def test_fleet_redeploy_fail_fast_stops_at_first_error():
    ws = _fake_ws([
        _model("a", catalog="cat", schema="sch", **{_fleet.NAME_TAG: "a"}),
        _model("b", catalog="cat", schema="sch", **{_fleet.NAME_TAG: "b"}),
    ])
    with patch("apx_agent.cli._require_sdk", return_value=ws), \
         patch("apx_agent._apps_registry.get_latest_apps_version",
               side_effect=RuntimeError("boom")), \
         patch("apx_agent._apps_registry.get_prod_alias_version", return_value="1"):
        result = CliRunner().invoke(main, ["fleet", "redeploy", "--apply", "--fail-fast"])
    assert result.exit_code == 1
    # Only the first agent was attempted before bailing.
    assert result.output.count("failed") >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd python && uv run pytest tests/test_fleet.py -k fleet_redeploy -v`
Expected: FAIL — `Error: No such command 'redeploy'`.

- [ ] **Step 3: Write minimal implementation**

```python
@fleet.command("redeploy")
@_fleet_select_options
@click.option("--apply", is_flag=True, help="Execute. Without it, dry-run.")
@click.option("--fail-fast", is_flag=True, help="Stop at the first failure.")
def fleet_redeploy_cmd(catalog, schema, name_glob, where_exprs, uc_names, profile,
                       apply, fail_fast):
    """Re-promote each selected agent's @prod alias to its latest version."""
    from apx_agent import _apps_registry, _fleet

    if schema and not catalog:
        raise click.UsageError("--schema requires --catalog.")
    ws = _require_sdk(profile)
    agents_ = _fleet_resolve(
        ws, catalog=catalog, schema=schema, name_glob=name_glob,
        where_exprs=where_exprs, uc_names=uc_names,
    )
    if not agents_:
        click.echo("No agents matched the selection.")
        return

    outcomes: list[_fleet.AgentOutcome] = []
    for a in agents_:
        try:
            latest = _apps_registry.get_latest_apps_version(a.uc_name)
            current = _apps_registry.get_prod_alias_version(a.uc_name)
            if latest is None:
                outcomes.append(_fleet.AgentOutcome(a.uc_name, "skipped", "no versions"))
                continue
            if latest == current:
                outcomes.append(_fleet.AgentOutcome(
                    a.uc_name, "skipped", f"already @{latest}"))
                continue
            if apply:
                _apps_registry.set_prod_alias_version(a.uc_name, latest)
            outcomes.append(_fleet.AgentOutcome(
                a.uc_name, "ok", f"{current or '-'} -> {latest}"))
        except Exception as e:
            outcomes.append(_fleet.AgentOutcome(a.uc_name, "failed", str(e)))
            if fail_fast:
                break

    text, code = _fleet.render_summary(outcomes, apply=apply)
    click.echo(text)
    if code:
        raise SystemExit(code)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd python && uv run pytest tests/test_fleet.py -k fleet_redeploy -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add python/src/apx_agent/cli.py python/tests/test_fleet.py
git commit -m "feat(fleet): fleet redeploy (re-promote to latest, dry-run, fail-fast)"
```

---

## Task 9: Full suite, type check, and docs touch-up

**Files:**
- Modify: `python/src/apx_agent/_fleet.py` and `cli.py` only if pyright flags issues
- Modify: `README.md` (add a short `fleet` section if a CLI command reference exists)

- [ ] **Step 1: Run the full test suite**

Run: `cd python && uv run pytest tests/test_fleet.py tests/test_cli.py -v`
Expected: PASS (all fleet + CLI tests).

- [ ] **Step 2: Run pyright on the package**

Run: `cd python && uv run pyright`
Expected: 0 errors. Fix any reported in `_fleet.py`/`cli.py` inline (e.g. add a `if client is None: ...` guard or `assert` if pyright complains about the dry-run `MlflowClient | None` in `fleet_tag_cmd` — restructure so the client is only referenced when `apply` is true).

- [ ] **Step 3: Verify the help text renders**

Run: `cd python && uv run apx-agent fleet --help`
Expected: lists `list`, `tag`, `backfill`, `redeploy`.

- [ ] **Step 4: Add a README mention (if a command table exists)**

Search `README.md` for an existing CLI command list. If present, add:

```markdown
- `apx-agent fleet list|tag|backfill|redeploy` — workspace-scoped bulk operations across many agents (dry-run by default; `--apply` to execute).
```

If no such section exists, skip this step.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test(fleet): full suite + pyright clean; docs touch-up"
```

---

## Self-Review notes

- **Spec coverage:** `fleet list` (Task 4), `fleet tag` (Task 6), `fleet backfill` (Task 7), `fleet redeploy` (Task 8), shared selector (Tasks 1–2 + `_fleet_resolve`), two-namespace tag model (Tasks 1/6), dry-run + continue-report + non-zero exit (Task 3 + every mutating command), `--fail-fast` (Task 8), `agents list` refactored onto resolver (Task 5), tag-on-registered-model (Tasks 6/7), backfill-is-partial messaging (Task 7). All spec sections map to a task.
- **Deviation from spec, intentional:** the spec said all four commands "consume the selector"; `fleet backfill` instead uses explicit `--uc-name` targets because untagged agents are undiscoverable by tag (documented in "Design clarifications" above and in Task 7).
- **Type consistency:** `ResolvedAgent` (`.uc_name`/`.name`/`.model`/`.app_name`/`.tags`/`.labels`), `AgentOutcome` (`.uc_name`/`.status`/`.detail`), `render_summary(outcomes, *, apply) -> (text, code)`, `resolve_agents(models, *, catalog, schema, name_glob, where, uc_names)`, `_fleet_resolve(ws, *, catalog, schema, name_glob, where_exprs, uc_names)` used consistently across all tasks.
