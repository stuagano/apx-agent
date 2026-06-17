# Governed UC Comment Write Tool — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Give an agent an **opt-in, governed** way to write Unity Catalog table/column `COMMENT`s — the OKF→UC write direction — as a declared agent tool that executes under the calling user's identity (OBO), so the write requires their UC `MODIFY` grant and is audited. NOT a CLI, NOT default-available.

**Architecture:** Mirror `sql_tool` (`sql_tools.py`): a `uc_comment_tool(...)` builder returns a `build_tool`-wrapped async callable that injects `UserClientDependency` (the per-call workspace client that **runs as the calling user**) and runs `COMMENT ON TABLE …` / `ALTER TABLE … ALTER COLUMN … COMMENT …` via `run_sql`. Register it as a new `[[tool.apx.tools]]` type `uc_comment_writer` in `_tool_config.py`'s `TOOL_TYPES` — so it exists only when an author explicitly declares it. The tool is **scoped to a declared `catalog.schema`** (blast radius), validates agent-supplied identifiers, escapes comment text, and surfaces permission errors instead of swallowing them.

**Tech Stack:** Python 3.11+, Databricks SDK (warehouse SQL exec via existing `run_sql`), pytest. Tests: `cd python && uv run pytest`.

**Context / decisions (user-confirmed 2026-06-17):**
- Mechanism = an apx agent tool running `COMMENT ON` via the warehouse under OBO (a literal UC *function* can't do DDL).
- Granularity = **per-write** (`update_uc_comment(table, comment, column?)`); bulk `sync` deferred.
- Gate = the caller's UC `MODIFY` grant (the tool surfaces permission errors); no separate confirmation step.
- Opt-in = present only when declared in `[[tool.apx.tools]]`; never wired by default.

**Relationship to OKF:** this is the governed write *primitive*. The "push curated OKF descriptions to UC" story = an OKF-grounded agent calls this tool with the curated text. No dependency on the OKF stack (#195–#198); builds on `sql_tool`/`run_sql`/`build_tool`/`TOOL_TYPES`, all pre-existing.

**Governance invariants (every task upholds):**
1. Runs as the **calling user** via `UserClientDependency` (OBO) — UC `MODIFY` grant is the gate.
2. **Scoped** to the author-declared `catalog.schema`; the agent supplies only `table`/`column`/`comment`.
3. Agent-supplied **identifiers validated** (`^[A-Za-z_][A-Za-z0-9_]*$`); comment text **escaped** (`'` → `''`). No SQL injection.
4. Errors (esp. permission) **returned as a result**, logged — never silently swallowed, never raised out of the tool.
5. **Opt-in** — not in any default tool set.

---

## Task 0: Worktree (already created by controller)
Worktree at `/Users/stuart.gano/Documents/apx-uc-comment-tool` on `feat/uc-comment-tool` (off `origin/main`). Verify: `cd python && uv run pytest tests/test_sql_tools.py -q` (or the nearest existing tool test) → green.

## Task 1: `uc_comment_tool` builder

**Files:** Create `python/src/apx_agent/uc_comment.py`; Test `python/tests/test_uc_comment.py`.

READ FIRST: `python/src/apx_agent/sql_tools.py` (the `sql_tool` structure — `UserClientDependency`, `build_tool`, `run_sql`, the NON-deferred-annotations note at the top, `ResourceSpec`), and `python/src/apx_agent/_sql.py` `run_sql` signature.

- [ ] **Step 1: Write the failing test `python/tests/test_uc_comment.py`**

```python
"""Tests for the governed UC comment write tool."""
import asyncio
from types import SimpleNamespace

from apx_agent.uc_comment import uc_comment_tool


def _call(tool, **kwargs):
    # build_tool returns a wrapped callable; invoke its underlying coroutine with a fake ws.
    fn = getattr(tool, "func", None) or getattr(tool, "fn", None) or tool
    captured = {}

    def fake_run_sql(ws, statement, warehouse_id=None):
        captured["statement"] = statement
        captured["warehouse_id"] = warehouse_id
        return []
    import apx_agent.uc_comment as mod
    mod.run_sql = fake_run_sql  # monkeypatch the module-level run_sql
    result = asyncio.get_event_loop().run_until_complete(fn(ws=SimpleNamespace(), **kwargs))
    return result, captured


def test_table_comment_statement():
    tool = uc_comment_tool(catalog="main", schema="sales", warehouse_id="wh1")
    result, cap = _call(tool, table="orders", comment="One row per order.")
    assert cap["statement"] == "COMMENT ON TABLE `main`.`sales`.`orders` IS 'One row per order.'"
    assert cap["warehouse_id"] == "wh1"
    assert result["status"] == "ok"


def test_column_comment_statement():
    tool = uc_comment_tool(catalog="main", schema="sales", warehouse_id="wh1")
    result, cap = _call(tool, table="orders", column="total_usd", comment="Order total in USD.")
    assert cap["statement"] == (
        "ALTER TABLE `main`.`sales`.`orders` ALTER COLUMN `total_usd` COMMENT 'Order total in USD.'"
    )
    assert result["status"] == "ok"


def test_quote_escaping():
    tool = uc_comment_tool(catalog="c", schema="s", warehouse_id="w")
    _, cap = _call(tool, table="t", comment="don't drop")
    assert "IS 'don''t drop'" in cap["statement"]  # single quote doubled


def test_invalid_identifier_rejected_no_sql():
    tool = uc_comment_tool(catalog="c", schema="s", warehouse_id="w")
    result, cap = _call(tool, table="orders; DROP TABLE x", comment="x")
    assert result["status"] == "error"
    assert "statement" not in cap  # never reached run_sql
    assert "identifier" in result["message"].lower()


def test_permission_error_surfaced_not_raised():
    tool = uc_comment_tool(catalog="c", schema="s", warehouse_id="w")
    fn = getattr(tool, "func", None) or getattr(tool, "fn", None) or tool
    import apx_agent.uc_comment as mod
    def boom(ws, statement, warehouse_id=None):
        raise PermissionError("PERMISSION_DENIED: requires MODIFY")
    mod.run_sql = boom
    result = asyncio.get_event_loop().run_until_complete(
        fn(ws=SimpleNamespace(), table="t", comment="x")
    )
    assert result["status"] == "error"
    assert "MODIFY" in result["message"]  # surfaced, not swallowed, not raised
```

NOTE: the exact way to reach the wrapped callable from `build_tool`'s return may differ — READ `build_tool` (`_tool_factory.py`) and adapt `_call`/`fn` extraction so the test invokes the real coroutine with an injected `ws`. If `build_tool` makes the inner fn hard to call directly, test via the same path the other tool tests use (find `test_sql_tools.py` and mirror its invocation harness).

- [ ] **Step 2: Run, expect FAIL** — `cd python && uv run pytest tests/test_uc_comment.py -q`.

- [ ] **Step 3: Create `python/src/apx_agent/uc_comment.py`** (mirror `sql_tools.py`; do NOT add `from __future__ import annotations` — same reason as sql_tools: `UserClientDependency` must resolve eagerly):

```python
"""uc_comment_tool — a governed, opt-in tool to write Unity Catalog COMMENTs.

Writes table/column comments via the SQL warehouse under the CALLING USER's
identity (OBO), so the write requires their UC ``MODIFY`` grant and is audited.
Scoped to a declared ``catalog.schema``. Opt-in only — declare it via
``[[tool.apx.tools]] type = "uc_comment_writer"``; never wired by default.

Annotations are intentionally NOT deferred so ``UserClientDependency`` resolves
eagerly (see sql_tools.py for the rationale).
"""

import logging
import re
from typing import Any

from ._sql import run_sql

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _esc_literal(text: str) -> str:
    return text.replace("'", "''")


def uc_comment_tool(
    *,
    catalog: str,
    schema: str,
    warehouse_id: str | None = None,
    name: str = "update_uc_comment",
    description: str | None = None,
) -> Any:
    """Return an opt-in tool that writes a UC table/column COMMENT.

    Scoped to ``catalog.schema``. Runs as the calling user (OBO) — the write
    needs their UC ``MODIFY`` grant. Args the agent supplies: ``table`` (within
    the declared schema), optional ``column``, and the ``comment`` text.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    _desc = description or (
        f"Write a Unity Catalog COMMENT on a table or column in "
        f"`{catalog}.{schema}`. Runs as the calling user — requires their UC "
        f"MODIFY grant. Provide `table`, an optional `column`, and the `comment` text."
    )

    async def _update_uc_comment(
        table: str,
        comment: str,
        ws: UserClientDependency,  # type: ignore[valid-type]
        column: str | None = None,
    ) -> dict[str, Any]:
        """Placeholder doc — overwritten below."""
        if not _IDENT.match(table or ""):
            return {"status": "error", "message": f"invalid table identifier: {table!r}"}
        if column is not None and not _IDENT.match(column):
            return {"status": "error", "message": f"invalid column identifier: {column!r}"}
        fqn = f"`{catalog}`.`{schema}`.`{table}`"
        lit = _esc_literal(comment or "")
        if column is None:
            stmt = f"COMMENT ON TABLE {fqn} IS '{lit}'"
        else:
            stmt = f"ALTER TABLE {fqn} ALTER COLUMN `{column}` COMMENT '{lit}'"
        try:
            run_sql(ws, stmt, warehouse_id=warehouse_id)
        except Exception as e:
            logger.warning("uc_comment write failed: %s", e)
            return {"status": "error", "statement": stmt, "message": str(e)}
        return {"status": "ok", "statement": stmt}

    return build_tool(
        _update_uc_comment,
        name=name,
        description=_desc,
        resources=[ResourceSpec("sql_warehouse", warehouse_id)] if warehouse_id else (),
    )
```

(If `build_tool`'s tool inspects parameter ORDER/annotations and the `ws` injected param must be last or have a specific position, mirror exactly how `sql_tool`'s `_run_sql(query, ws)` orders it — adjust the signature so the injected `ws` is discovered the same way. The test must drive the real wrapped callable.)

- [ ] **Step 4: Run, expect PASS** — `cd python && uv run pytest tests/test_uc_comment.py -q`.
- [ ] **Step 5: Commit**
```bash
git add python/src/apx_agent/uc_comment.py python/tests/test_uc_comment.py
git commit -m "feat(tools): uc_comment_tool — governed OBO write of UC table/column COMMENTs"
```

## Task 2: Register the `uc_comment_writer` declarative tool type

**Files:** Modify `python/src/apx_agent/_tool_config.py` (the `TOOL_TYPES` registry func ~line 118); possibly export from `python/src/apx_agent/__init__.py`; Test `python/tests/test_tool_config.py`.

- [ ] **Step 1: Failing test** that a `[[tool.apx.tools]]`-style table `{"type": "uc_comment_writer", "catalog": "c", "schema": "s", "warehouse_id": "w"}` builds a tool via the same path the other types use, and that the type is in the registry; and that it is NOT in any default tool set. READ `test_tool_config.py` to mirror how `genie`/`uc_function` types are tested.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3:** in the `TOOL_TYPES` dict, add `from .uc_comment import uc_comment_tool` to the lazy imports and `"uc_comment_writer": uc_comment_tool,` to the returned dict. If apx exports tool builders from `__init__.py` (check), export `uc_comment_tool` too for programmatic use.
- [ ] **Step 4: Run, expect PASS** — `cd python && uv run pytest tests/test_tool_config.py tests/test_uc_comment.py -q`.
- [ ] **Step 5: Commit**
```bash
git add python/src/apx_agent/_tool_config.py python/src/apx_agent/__init__.py python/tests/test_tool_config.py
git commit -m "feat(tools): register uc_comment_writer declarative tool type"
```

## Task 3: Docs + opt-in/governance integration test

**Files:** Modify `docs/reference/configuration.md` (or the tools reference doc); Test `python/tests/test_uc_comment.py` (integration).

- [ ] **Step 1:** Add a docs subsection for `[[tool.apx.tools]] type = "uc_comment_writer"`: what it does, that it is **opt-in** (never default), runs as the **calling user** (needs UC `MODIFY`), is **scoped** to the declared `catalog.schema`, and validates identifiers/escapes text. Note the OKF tie-in: an OKF-grounded agent can use it to push curated descriptions to UC. Match the doc's existing `[[tool.apx.tools]]` style.
- [ ] **Step 2:** Add an integration test: declare the tool via the config path (as in Task 2) and drive a table-comment write end-to-end through the built tool with a fake `ws`, asserting the `COMMENT ON TABLE` statement and `status="ok"`; and an assertion that no default/agentless construction includes this tool (opt-in). Reuse the fake-`ws` harness from Task 1.
- [ ] **Step 3: Run** `cd python && uv run pytest tests/test_uc_comment.py tests/test_tool_config.py -q`; then full suite `cd python && uv run pytest -q 2>&1 | grep -E "passed|failed" | tail -2` → 0 failures.
- [ ] **Step 4: Commit**
```bash
git add docs/reference/configuration.md python/tests/test_uc_comment.py
git commit -m "docs(tools): document the opt-in governed uc_comment_writer tool"
```

---

## Self-review notes
- **Governance invariants** (OBO via `UserClientDependency`; scoped to declared `catalog.schema`; identifier validation + quote escaping; errors surfaced not swallowed; opt-in) are each pinned by a Task-1 test (`test_invalid_identifier_rejected_no_sql`, `test_quote_escaping`, `test_permission_error_surfaced_not_raised`) and the Task-3 opt-in assertion.
- **No CLI** and **no default wiring** — the only entry is an explicit `[[tool.apx.tools]]` declaration (Task 2/3), honoring the governance decision that UC writes are deliberate, granted, audited capabilities.
- **Mirrors `sql_tool`** for the OBO mechanism — no new identity/auth code, reusing `UserClientDependency`/`run_sql`/`build_tool`.
- **Deferred:** a bulk `sync-okf-to-uc` flow that iterates a bundle's curated descriptions and calls this tool; column-comment idempotency/diffing; writing UC tags.
