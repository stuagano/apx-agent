"""Step 1 of golden/verified queries (#288): the ``okf_grounding`` parser emits
structured ``golden_queries`` from a table's ``# Examples`` section, while the
legacy ``examples`` string — and therefore the served prompt — stays unchanged.
"""
from __future__ import annotations

from apx_agent._okf import (
    _parse_golden_queries,
    okf_grounding,
    write_okf_bundle,
)
from apx_agent._schema import build_instructions_from_schema


# ── _parse_golden_queries: the pure parser ───────────────────────────────────


def test_single_question_with_params():
    section = """
### How many pay runs last month?
```sql
SELECT count(*) FROM {catalog}.{schema}.pay_runs WHERE run_date >= @start AND run_date < @end
```
- @start (date): first day of the period
- @end (date): day after the period
"""
    gq = _parse_golden_queries(section)
    assert len(gq) == 1
    assert gq[0]["question"] == "How many pay runs last month?"
    assert "SELECT count(*)" in gq[0]["sql"] and "@start" in gq[0]["sql"]
    assert gq[0]["params"] == [
        {"name": "start", "type": "date", "desc": "first day of the period"},
        {"name": "end", "type": "date", "desc": "day after the period"},
    ]


def test_multiple_questions_kept_in_order():
    section = """
### First question
```sql
SELECT 1
```

### Second question
```sql
SELECT 2
```
"""
    gq = _parse_golden_queries(section)
    assert [q["question"] for q in gq] == ["First question", "Second question"]
    assert [q["sql"] for q in gq] == ["SELECT 1", "SELECT 2"]
    assert gq[0]["params"] == []


def test_bare_code_block_is_legacy_unlabelled_example():
    # No ``###`` subheadings — the freeform pre-golden-query form must still parse
    # as one entry with question=None (backward compatibility).
    section = """
```sql
SELECT * FROM t
```
"""
    gq = _parse_golden_queries(section)
    assert gq == [{"question": None, "sql": "SELECT * FROM t", "params": []}]


def test_preamble_block_and_subheadings_both_captured():
    section = """
```sql
SELECT 0
```

### Labelled
```sql
SELECT 1
```
"""
    gq = _parse_golden_queries(section)
    assert [q["question"] for q in gq] == [None, "Labelled"]
    assert [q["sql"] for q in gq] == ["SELECT 0", "SELECT 1"]


def test_subheading_without_sql_is_skipped():
    section = """
### A question with no query yet
just prose, no code block

### Has a query
```sql
SELECT 1
```
"""
    gq = _parse_golden_queries(section)
    assert [q["question"] for q in gq] == ["Has a query"]


def test_empty_section_returns_empty():
    assert _parse_golden_queries("") == []
    assert _parse_golden_queries("   \n  ") == []


def test_star_bullet_params_and_extra_text_tolerated():
    section = """
### Q
```sql
SELECT @x
```
* @x (string): a value
- not a param line
- @y (int): another
"""
    gq = _parse_golden_queries(section)
    assert gq[0]["params"] == [
        {"name": "x", "type": "string", "desc": "a value"},
        {"name": "y", "type": "int", "desc": "another"},
    ]


# ── okf_grounding integration + the step-1 non-breaking invariant ─────────────


_EXAMPLES_MD = """
# Examples

### How many orders over $100?
```sql
SELECT count(*) FROM {catalog}.{schema}.orders WHERE amount > @threshold
```
- @threshold (int): the dollar amount

### Total amount by id
```sql
SELECT id, sum(amount) FROM {catalog}.{schema}.orders GROUP BY id
```
"""


def _bundle_with_golden_queries(tmp_path):
    manifest = {"catalog": "c", "schema": "s", "tables": {"orders": ["id", "amount"]}}
    write_okf_bundle(manifest, tmp_path / "okf", timestamp="z")
    orders_md = tmp_path / "okf" / "tables" / "orders.md"
    orders_md.write_text(orders_md.read_text().rstrip() + "\n\n" + _EXAMPLES_MD)
    return manifest


def test_okf_grounding_emits_golden_queries(tmp_path):
    manifest = _bundle_with_golden_queries(tmp_path)
    grounding = okf_grounding(tmp_path / "okf")
    assert grounding is not None and "orders" in grounding
    gq = grounding["orders"]["golden_queries"]
    assert [q["question"] for q in gq] == ["How many orders over $100?", "Total amount by id"]
    assert gq[0]["params"] == [{"name": "threshold", "type": "int", "desc": "the dollar amount"}]
    # The legacy `examples` string (first code block) is still emitted unchanged.
    assert "SELECT count(*)" in grounding["orders"]["examples"]


def test_step2_renders_golden_queries_as_labelled_pairs(tmp_path):
    # Step 2: the renderer now emits labelled ``Q: <question>`` → SQL few-shot
    # pairs from golden_queries into the served instructions.
    manifest = _bundle_with_golden_queries(tmp_path)
    grounding = okf_grounding(tmp_path / "okf")
    out = build_instructions_from_schema(
        manifest["catalog"], manifest["schema"], manifest["tables"], grounding=grounding
    )
    assert "Q: How many orders over $100?" in out
    assert "Q: Total amount by id" in out
    assert "SELECT count(*) FROM" in out
    assert "golden_queries" not in out  # the key name never leaks into the prompt


def test_step2_bare_example_renders_byte_identically(tmp_path):
    # A bare-code-block ``# Examples`` (question=None) must still render as the
    # pre-golden-query ``Example:`` form — no ``Q:`` label, no behaviour change.
    manifest = {"catalog": "c", "schema": "s", "tables": {"orders": ["id", "amount"]}}
    write_okf_bundle(manifest, tmp_path / "okf", timestamp="z")
    orders_md = tmp_path / "okf" / "tables" / "orders.md"
    orders_md.write_text(
        orders_md.read_text().rstrip()
        + "\n\n# Examples\n```sql\nSELECT * FROM orders\n```\n"
    )
    grounding = okf_grounding(tmp_path / "okf")
    out = build_instructions_from_schema(
        manifest["catalog"], manifest["schema"], manifest["tables"], grounding=grounding
    )
    assert "    Example:" in out
    assert "      SELECT * FROM orders" in out
    assert "Q:" not in out
