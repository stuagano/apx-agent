"""Glossary / synonyms layer (#296 phase 1): okf_glossary parses
``{term, definition, synonyms}`` from the bundle's dataset-level ``# Glossary``
section. Parse-only — the prompt is unchanged until the renderer (phase 2)."""
from __future__ import annotations

from apx_agent._okf import _parse_glossary, okf_glossary, write_okf_bundle


# ── _parse_glossary: the pure parser ─────────────────────────────────────────


def test_single_term_with_inline_synonyms():
    section = """
### Gross pay
Total earnings before deductions. Synonyms: gross, gross earnings, gross wages.
"""
    assert _parse_glossary(section) == [{
        "term": "Gross pay",
        "definition": "Total earnings before deductions.",
        "synonyms": ["gross", "gross earnings", "gross wages"],
    }]


def test_multiple_terms_in_order():
    section = """
### Pay run
A single payroll processing cycle.

### Net pay
Take-home pay after deductions.
Synonyms: net; take-home
"""
    g = _parse_glossary(section)
    assert [e["term"] for e in g] == ["Pay run", "Net pay"]
    assert g[0]["synonyms"] == [] and g[0]["definition"] == "A single payroll processing cycle."
    assert g[1]["synonyms"] == ["net", "take-home"]
    assert g[1]["definition"] == "Take-home pay after deductions."


def test_synonyms_case_insensitive_and_separators():
    g = _parse_glossary("### Term\nA def.\nSYNONYM: a , b; c")
    assert g[0]["synonyms"] == ["a", "b", "c"]


def test_empty_and_termless_entries_skipped():
    assert _parse_glossary("") == []
    assert _parse_glossary("   \n") == []
    assert _parse_glossary("### Term with no body\n") == []  # no definition → skipped


# ── okf_glossary: reads the dataset-level doc ────────────────────────────────


def _bundle(tmp_path):
    write_okf_bundle(
        {"catalog": "c", "schema": "s", "tables": {"orders": ["id(INT)"]}},
        tmp_path / "okf", timestamp="z",
    )
    return tmp_path / "okf"


def test_okf_glossary_reads_dataset_doc(tmp_path):
    okf = _bundle(tmp_path)
    ds = okf / "datasets" / "s.md"
    ds.write_text(
        ds.read_text().rstrip()
        + "\n\n# Glossary\n\n### Gross pay\nEarnings before deductions. Synonyms: gross\n"
    )
    assert okf_glossary(okf) == [
        {"term": "Gross pay", "definition": "Earnings before deductions.", "synonyms": ["gross"]}
    ]


def test_okf_glossary_empty_without_section(tmp_path):
    assert okf_glossary(_bundle(tmp_path)) == []


def test_okf_glossary_totalised_on_missing_bundle(tmp_path):
    assert okf_glossary(tmp_path / "nope") == []
