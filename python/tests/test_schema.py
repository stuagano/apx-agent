"""Tests for _schema.py — manifest loading, Tables-API introspection, grounding."""
from __future__ import annotations

import json
from pathlib import Path

from apx_agent._schema import load_baked_schema, APX_DIR, SCHEMA_MANIFEST_NAME


def _write_manifest(root: Path, manifest: dict) -> None:
    d = root / APX_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / SCHEMA_MANIFEST_NAME).write_text(json.dumps(manifest))


class TestLoadBakedSchema:
    def test_loads_from_start_dir(self, tmp_path):
        m = {"catalog": "samples", "schema": "tpch", "tables": {"customer": ["c_custkey(bigint)"]}}
        _write_manifest(tmp_path, m)
        assert load_baked_schema(tmp_path) == m

    def test_walks_up_from_nested_dir(self, tmp_path):
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        _write_manifest(tmp_path, m)
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert load_baked_schema(nested) == m

    def test_missing_returns_none(self, tmp_path):
        assert load_baked_schema(tmp_path) is None

    def test_corrupt_json_returns_none(self, tmp_path):
        d = tmp_path / APX_DIR
        d.mkdir()
        (d / SCHEMA_MANIFEST_NAME).write_text("{not valid json")
        assert load_baked_schema(tmp_path) is None


class TestIntrospectViaTablesApi:
    def test_builds_table_to_columns_map(self):
        from types import SimpleNamespace
        from apx_agent._schema import introspect_schema_columns

        def col(name, type_text):
            return SimpleNamespace(name=name, type_text=type_text)

        tables = [
            SimpleNamespace(name="customer", columns=[col("c_custkey", "bigint"), col("c_name", "string")]),
            SimpleNamespace(name="orders", columns=[col("o_orderkey", "bigint")]),
        ]
        ws = SimpleNamespace(tables=SimpleNamespace(list=lambda catalog_name, schema_name: tables))
        out = introspect_schema_columns(ws, "samples", "tpch")
        assert out == {
            "customer": ["c_custkey(bigint)", "c_name(string)"],
            "orders": ["o_orderkey(bigint)"],
        }

    def test_returns_empty_on_failure(self):
        from types import SimpleNamespace
        from apx_agent._schema import introspect_schema_columns

        def boom(**_):
            raise RuntimeError("no perms")
        ws = SimpleNamespace(tables=SimpleNamespace(list=boom))
        assert introspect_schema_columns(ws, "c", "s") == {}

    def test_none_ws_returns_empty(self):
        from apx_agent._schema import introspect_schema_columns
        assert introspect_schema_columns(None, "c", "s") == {}


class TestBuildInstructions:
    DISCOVERY = "call the SQL tool to confirm what tables and columns are available"

    def test_grounded_lists_columns_and_drops_discovery(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {
            "customer": ["c_custkey(bigint)", "c_name(string)", "c_acctbal(decimal)"],
            "orders": ["o_orderkey(bigint)", "o_custkey(bigint)"],
        }
        out = build_instructions_from_schema("samples", "tpch", tables)
        # Lists real tables + columns
        assert "customer" in out and "c_custkey(bigint)" in out
        assert "orders" in out and "o_orderkey(bigint)" in out
        # Drops the "go discover the schema" instruction
        assert self.DISCOVERY not in out
        # Tells it not to rediscover
        assert "SHOW TABLES" in out

    def test_ungrounded_keeps_discovery(self):
        from apx_agent._schema import build_instructions_from_schema
        out = build_instructions_from_schema("samples", "tpch", {})
        assert self.DISCOVERY in out  # no tables known → still tells it to discover

    def test_column_cap(self):
        from apx_agent._schema import build_instructions_from_schema
        cols = [f"col{i}(int)" for i in range(40)]
        out = build_instructions_from_schema("c", "s", {"big": cols})
        assert "col0(int)" in out
        assert "col39(int)" not in out      # capped
        assert "more" in out.lower()        # "+N more" hint


class TestPersona:
    def test_persona_leads_grounded_instructions(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"customer": ["c_custkey(bigint)", "c_name(string)"]}
        out = build_instructions_from_schema("samples", "tpch", tables,
                                             persona="a revenue analyst")
        assert out.startswith("You are a revenue analyst.")
        # grounding is intact
        assert "customer" in out and "c_custkey(bigint)" in out
        assert "SHOW TABLES" in out

    def test_no_persona_keeps_default_lead(self):
        from apx_agent._schema import build_instructions_from_schema
        out = build_instructions_from_schema("samples", "tpch",
                                             {"customer": ["c_custkey(bigint)"]})
        assert out.startswith("You are a data assistant for samples.tpch.")

    def test_persona_on_ungrounded_too(self):
        from apx_agent._schema import build_instructions_from_schema
        out = build_instructions_from_schema("samples", "tpch", {},
                                             persona="a revenue analyst")
        assert out.startswith("You are a revenue analyst.")
        assert "confirm what tables and columns are available" in out  # still discovers


class TestLoadBakedSchemaOKF:
    def _write_okf(self, root, manifest):
        from apx_agent._okf import write_okf_bundle
        import shutil
        write_okf_bundle(manifest, root / "okf_tmp", timestamp="z")
        dest = root / ".apx" / "okf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(root / "okf_tmp"), str(dest))

    def test_prefers_okf_bundle(self, tmp_path):
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        self._write_okf(tmp_path, m)
        assert load_baked_schema(tmp_path) == m

    def test_okf_wins_over_stale_schema_json(self, tmp_path):
        stale = {"catalog": "c", "schema": "s", "tables": {"old": ["x(int)"]}}
        _write_manifest(tmp_path, stale)
        fresh = {"catalog": "c", "schema": "s", "tables": {"new": ["y(int)"]}}
        self._write_okf(tmp_path, fresh)
        assert load_baked_schema(tmp_path) == fresh

    def test_falls_back_to_schema_json_when_okf_malformed(self, tmp_path):
        cache = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        _write_manifest(tmp_path, cache)
        okf = tmp_path / ".apx" / "okf" / "datasets"
        okf.mkdir(parents=True)
        (okf / "x.md").write_text("---\n: bad: yaml\n---\n")
        assert load_baked_schema(tmp_path) == cache

    def test_warns_on_okf_parse_failure(self, tmp_path, caplog):
        import logging
        cache = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        _write_manifest(tmp_path, cache)
        okf = tmp_path / ".apx" / "okf" / "datasets"
        okf.mkdir(parents=True)
        (okf / "x.md").write_text("---\n: bad: yaml\n---\n")
        with caplog.at_level(logging.WARNING):
            assert load_baked_schema(tmp_path) == cache
        assert any("did not parse" in r.message for r in caplog.records)

    def test_no_warning_on_happy_path(self, tmp_path, caplog):
        import logging
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        self._write_okf(tmp_path, m)
        with caplog.at_level(logging.WARNING):
            assert load_baked_schema(tmp_path) == m
        assert not any("did not parse" in r.message for r in caplog.records)


class TestLoadOKFGrounding:
    def _write_enriched_okf(self, root):
        from apx_agent._okf import write_okf_bundle
        import shutil
        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        tmp = root / "okf_tmp"
        write_okf_bundle(m, tmp, timestamp="z")
        (tmp / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nPay records.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        dest = root / ".apx" / "okf"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))

    def test_returns_enrichment_when_present(self, tmp_path):
        from apx_agent._schema import load_okf_grounding
        self._write_enriched_okf(tmp_path)
        g = load_okf_grounding(tmp_path)
        assert g is not None and g["pay_runs"]["description"] == "Pay records."

    def test_returns_none_when_no_okf(self, tmp_path):
        from apx_agent._schema import load_okf_grounding
        assert load_okf_grounding(tmp_path) is None


class TestGroundedInstructions:
    def test_default_identical_when_grounding_none(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"pay_runs": ["gross_pay(decimal(6,2))"], "employees": ["employee_id(string)"]}
        a = build_instructions_from_schema("c", "s", tables)
        b = build_instructions_from_schema("c", "s", tables, grounding=None)
        c = build_instructions_from_schema("c", "s", tables, grounding={})
        assert a == b == c

    def test_unenriched_table_line_unchanged_when_other_enriched(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"pay_runs": ["gross_pay(decimal(6,2))"], "employees": ["employee_id(string)"]}
        grounding = {"pay_runs": {"description": "Pay records.", "columns": [], "joins": "", "examples": ""}}
        out = build_instructions_from_schema("c", "s", tables, grounding=grounding)
        assert "- pay_runs: gross_pay(decimal(6,2))" in out
        assert "    Pay records." in out
        assert "- employees: employee_id(string)" in out

    def test_enrichment_includes_joins_and_example(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"pay_runs": ["gross_pay(decimal(6,2))"]}
        grounding = {"pay_runs": {
            "description": "", "columns": [{"name": "gross_pay", "type": "decimal(6,2)", "description": "Gross."}],
            "joins": "Join employees on employee_id.", "examples": "SELECT * FROM pay_runs",
        }}
        out = build_instructions_from_schema("c", "s", tables, grounding=grounding)
        assert "    - gross_pay: Gross." in out
        assert "    Joins: Join employees on employee_id." in out
        assert "SELECT * FROM pay_runs" in out

    def test_multiline_description_and_joins_are_first_line_only(self):
        from apx_agent._schema import build_instructions_from_schema
        tables = {"pay_runs": ["gross_pay(decimal(6,2))"]}
        grounding = {"pay_runs": {
            "description": "Line one.\nLine two should not leak.",
            "columns": [], "joins": "Join A.\nJoin B should not leak.", "examples": "",
        }}
        out = build_instructions_from_schema("c", "s", tables, grounding=grounding)
        assert "    Line one." in out
        assert "Line two should not leak." not in out
        assert "    Joins: Join A." in out
        assert "Join B should not leak." not in out


class TestLoadGroundingFromPath:
    def test_reads_manifest_and_grounding_from_explicit_bundle(self, tmp_path):
        from apx_agent._okf import write_okf_bundle
        from apx_agent._schema import load_grounding_from_path
        m = {"catalog": "c", "schema": "s", "tables": {"t": ["a(int)"]}}
        write_okf_bundle(m, tmp_path / "okf", timestamp="z")
        manifest, grounding = load_grounding_from_path(tmp_path / "okf")
        assert manifest == m
        assert grounding is None  # bare bundle, no enrichment

    def test_reads_enrichment_when_present(self, tmp_path):
        from apx_agent._okf import write_okf_bundle
        from apx_agent._schema import load_grounding_from_path
        m = {"catalog": "c", "schema": "s", "tables": {"pay_runs": ["gross_pay(decimal(6,2))"]}}
        write_okf_bundle(m, tmp_path / "okf", timestamp="z")
        (tmp_path / "okf" / "tables" / "pay_runs.md").write_text(
            "---\ntype: Unity Catalog Table\ntitle: pay_runs\ndescription: d\ntimestamp: z\n---\n\n"
            "# Overview\nPay records.\n\n# Schema\n| Column | Type | Description |\n| --- | --- | --- |\n"
            "| `gross_pay` | decimal(6,2) |  |\n"
        )
        manifest, grounding = load_grounding_from_path(tmp_path / "okf")
        assert manifest["tables"] == {"pay_runs": ["gross_pay(decimal(6,2))"]}
        assert grounding is not None and grounding["pay_runs"]["description"] == "Pay records."

    def test_missing_path_returns_none_none(self, tmp_path):
        from apx_agent._schema import load_grounding_from_path
        assert load_grounding_from_path(tmp_path / "nope") == (None, None)
