"""Tests for the vendored OKF v0.1 reader/writer (_okf.py)."""
from __future__ import annotations

from apx_agent._okf import OKFDocument, REQUIRED_FRONTMATTER_KEYS


class TestOKFDocument:
    def test_parse_roundtrip(self):
        text = (
            "---\n"
            "type: Unity Catalog Table\n"
            "title: pay_runs\n"
            "description: One row per employee per pay period.\n"
            "timestamp: '2026-06-16T00:00:00+00:00'\n"
            "---\n\n"
            "# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `run_id` | string |  |\n"
        )
        doc = OKFDocument.parse(text)
        assert doc.frontmatter["type"] == "Unity Catalog Table"
        assert doc.frontmatter["title"] == "pay_runs"
        assert doc.body.startswith("# Schema")
        again = OKFDocument.parse(doc.serialize())
        assert again.frontmatter == doc.frontmatter
        assert again.body.strip() == doc.body.strip()

    def test_parse_no_frontmatter_is_tolerant(self):
        doc = OKFDocument.parse("# Just a body\nno frontmatter")
        assert doc.frontmatter == {}
        assert "Just a body" in doc.body

    def test_body_with_horizontal_rule_not_split_as_frontmatter(self):
        text = "---\ntype: X\ntitle: t\ndescription: d\ntimestamp: z\n---\n\n# Schema\n| --- |\n| `a` | int |\n"
        doc = OKFDocument.parse(text)
        assert doc.frontmatter["type"] == "X"
        assert "| `a` | int |" in doc.body

    def test_validate_requires_keys_emit_side(self):
        import pytest
        doc = OKFDocument(frontmatter={"type": "X"}, body="")
        with pytest.raises(ValueError):
            doc.validate()
        OKFDocument(
            frontmatter={"type": "X", "title": "t", "description": "d", "timestamp": "z"},
            body="",
        ).validate()


class TestParseSchemaColumns:
    def test_pipe_table_extracts_col_and_type(self):
        from apx_agent._okf import parse_schema_columns
        body = (
            "# Schema\n"
            "| Column | Type | Description |\n"
            "| --- | --- | --- |\n"
            "| `run_id` | string |  |\n"
            "| `gross_pay` | decimal(6,2) | Gross pay. |\n"
            "| `tags` | array<string> |  |\n"
        )
        assert parse_schema_columns(body) == [
            "run_id(string)",
            "gross_pay(decimal(6,2))",
            "tags(array<string>)",
        ]

    def test_fk_link_in_description_does_not_pollute_name(self):
        from apx_agent._okf import parse_schema_columns
        body = (
            "# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `employee_id` | string | FK -> [`employees`](/tables/employees.md) |\n"
        )
        assert parse_schema_columns(body) == ["employee_id(string)"]

    def test_missing_schema_section_returns_empty(self):
        from apx_agent._okf import parse_schema_columns
        assert parse_schema_columns("# Overview\nno schema here") == []

    def test_stops_at_next_heading(self):
        from apx_agent._okf import parse_schema_columns
        body = (
            "# Schema\n| Column | Type |\n| --- | --- |\n| `a` | int |\n"
            "# Joins\n| `not_a_col` | nope |\n"
        )
        assert parse_schema_columns(body) == ["a(int)"]

    def test_bullet_form_best_effort(self):
        from apx_agent._okf import parse_schema_columns
        body = "# Schema\n- `event_date` (STRING): the date\n"
        assert parse_schema_columns(body) == ["event_date(STRING)"]
