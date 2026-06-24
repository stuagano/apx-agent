"""Glossary/synonyms layer (#296 phase 2): the glossary renders into the served
prompt (build_instructions_from_schema) and is loaded + threaded by DataAgent.
Byte-identical when there's no glossary."""
from __future__ import annotations

from apx_agent._okf import write_okf_bundle
from apx_agent._schema import (
    _format_glossary_block,
    build_instructions_from_schema,
    load_okf_glossary,
)

GLOSS = [
    {"term": "Gross pay", "definition": "Earnings before deductions.",
     "synonyms": ["gross", "gross wages"]},
    {"term": "Pay run", "definition": "A payroll cycle.", "synonyms": []},
]
TABLES = {"orders": ["id(INT)", "amount(DOUBLE)"]}


def test_format_glossary_block():
    lines = _format_glossary_block(GLOSS).splitlines()
    assert lines[0] == "- Gross pay: Earnings before deductions. (also: gross, gross wages)"
    assert lines[1] == "- Pay run: A payroll cycle."  # no synonyms → no "(also:)"


def test_glossary_renders_into_grounded_instructions():
    out = build_instructions_from_schema("c", "s", TABLES, glossary=GLOSS)
    assert "Business glossary" in out
    assert "Gross pay: Earnings before deductions. (also: gross, gross wages)" in out


def test_glossary_renders_in_ungrounded_path():
    out = build_instructions_from_schema("c", "s", {}, glossary=GLOSS)  # no tables
    assert "Business glossary" in out and "Gross pay" in out


def test_prompt_byte_identical_without_glossary():
    # Grounded and ungrounded both unchanged when glossary is absent/empty.
    for tables in (TABLES, {}):
        base = build_instructions_from_schema("c", "s", tables)
        assert base == build_instructions_from_schema("c", "s", tables, glossary=None)
        assert base == build_instructions_from_schema("c", "s", tables, glossary=[])
        assert "Business glossary" not in base


def _bundle_with_glossary(root):
    write_okf_bundle(
        {"catalog": "c", "schema": "s", "tables": {"orders": ["id(INT)"]}},
        root, timestamp="z",
    )
    ds = root / "datasets" / "s.md"
    ds.write_text(
        ds.read_text().rstrip()
        + "\n\n# Glossary\n\n### Gross pay\nEarnings before deductions. Synonyms: gross\n"
    )


def test_load_okf_glossary_walks_up_to_bundle(tmp_path):
    _bundle_with_glossary(tmp_path / ".apx" / "okf")
    assert load_okf_glossary(tmp_path) == [
        {"term": "Gross pay", "definition": "Earnings before deductions.", "synonyms": ["gross"]}
    ]


def test_dataagent_renders_glossary_from_knowledge_bundle(tmp_path):
    from apx_agent import DataAgent

    _bundle_with_glossary(tmp_path / "okf")
    agent = DataAgent("c", "s", knowledge=str(tmp_path / "okf"))
    assert "Business glossary" in agent._instructions
    assert "Gross pay: Earnings before deductions. (also: gross)" in agent._instructions
