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
