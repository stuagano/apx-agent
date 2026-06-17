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
