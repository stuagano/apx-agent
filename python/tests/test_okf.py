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


class TestOKFManifest:
    def _write_bundle(self, root, *, tables_order):
        okf = root / ".apx" / "okf"
        (okf / "datasets").mkdir(parents=True)
        (okf / "tables").mkdir(parents=True)
        (okf / "datasets" / "payroll_demo.md").write_text(
            "---\ntype: Databricks Schema\ntitle: payroll_demo\n"
            "description: d\ncatalog: cat\nschema: payroll_demo\ntimestamp: z\n---\n\n# Tables\n"
        )
        for t, cols in tables_order:
            rows = "".join(f"| `{c}` | int |  |\n" for c in cols)
            (okf / "tables" / f"{t}.md").write_text(
                f"---\ntype: Unity Catalog Table\ntitle: {t}\ndescription: d\ntimestamp: z\n---\n\n"
                f"# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n{rows}"
            )
        return okf

    def test_parses_catalog_schema_tables(self, tmp_path):
        from apx_agent._okf import okf_manifest
        okf = self._write_bundle(tmp_path, tables_order=[("employees", ["a", "b"]), ("pay_runs", ["c"])])
        out = okf_manifest(okf)
        assert out["catalog"] == "cat"
        assert out["schema"] == "payroll_demo"
        assert out["tables"] == {"employees": ["a(int)", "b(int)"], "pay_runs": ["c(int)"]}

    def test_excludes_reserved_index_md_no_phantom_table(self, tmp_path):
        from apx_agent._okf import okf_manifest
        okf = self._write_bundle(tmp_path, tables_order=[("employees", ["a"])])
        (okf / "tables" / "index.md").write_text("# Tables\n* [employees](employees.md)\n")
        out = okf_manifest(okf)
        assert "index" not in out["tables"]
        assert set(out["tables"]) == {"employees"}

    def test_missing_dataset_returns_none(self, tmp_path):
        from apx_agent._okf import okf_manifest
        okf = tmp_path / ".apx" / "okf" / "tables"
        okf.mkdir(parents=True)
        assert okf_manifest(tmp_path / ".apx" / "okf") is None

    def test_malformed_bundle_returns_none_never_raises(self, tmp_path):
        from apx_agent._okf import okf_manifest
        okf = tmp_path / ".apx" / "okf"
        (okf / "datasets").mkdir(parents=True)
        (okf / "datasets" / "x.md").write_text("---\nnot: : valid: yaml\n: -\n---\n")
        assert okf_manifest(okf) is None


class TestWriteOKFBundle:
    def test_roundtrip_manifest(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, okf_manifest
        manifest = {
            "catalog": "serverless_stable_qh44kx_catalog",
            "schema": "payroll_demo",
            "tables": {
                "employees": ["employee_id(string)", "hire_date(date)"],
                "pay_runs": ["gross_pay(decimal(6,2))", "tags(array<string>)"],
            },
        }
        okf = tmp_path / ".apx" / "okf"
        write_okf_bundle(manifest, okf, timestamp="2026-06-16T00:00:00+00:00")
        assert okf_manifest(okf) == manifest

    def test_emitted_concepts_are_okf_conformant(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, OKFDocument, REQUIRED_FRONTMATTER_KEYS
        write_okf_bundle(
            {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}},
            tmp_path, timestamp="z",
        )
        for md in (tmp_path / "tables").glob("*.md"):
            if md.name in {"index.md", "log.md"}:
                continue
            fm = OKFDocument.parse(md.read_text()).frontmatter
            assert all(fm.get(k) for k in REQUIRED_FRONTMATTER_KEYS)

    def test_pipe_in_comment_is_escaped(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, okf_manifest
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["x(string)"]}}
        write_okf_bundle(m, tmp_path, timestamp="z", descriptions={"t": {"x": "a | b"}})
        assert okf_manifest(tmp_path)["tables"]["t"] == ["x(string)"]


class TestNonAlphabeticalOrder:
    def test_index_md_pins_non_alphabetical_order(self, tmp_path):
        from apx_agent._okf import write_okf_bundle, okf_manifest
        from apx_agent._schema import build_instructions_from_schema

        # pay_runs BEFORE employees — deliberately NOT sorted() order.
        m = {
            "catalog": "c",
            "schema": "s",
            "tables": {
                "pay_runs": ["gross_pay(decimal(6,2))"],
                "employees": ["employee_id(string)"],
            },
        }
        okf = tmp_path / "okf"
        write_okf_bundle(m, okf, timestamp="z")
        out = okf_manifest(okf)
        # tables/index.md (written in manifest order) must pin the non-alpha order,
        # overriding sorted() which would give [employees, pay_runs].
        assert list(out["tables"].keys()) == ["pay_runs", "employees"]
        before = build_instructions_from_schema(m["catalog"], m["schema"], m["tables"])
        after = build_instructions_from_schema(m["catalog"], m["schema"], out["tables"])
        assert after == before  # order-sensitive prompt stays byte-identical
