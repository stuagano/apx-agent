"""Tests for document_extract_tool — declared parse → extract, no live warehouse."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apx_agent import ToolError, document_extract_tool
from apx_agent._resources import ResourceSpec, get_resources
from apx_agent._tool_config import ToolConfigError, load_config_tools
from apx_agent.document_extract import bind_volume_path


SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}
VOLUME = "main.contracts.raw_contracts"
VOLUME_ROOT = "/Volumes/main/contracts/raw_contracts"


def _tool(**kwargs):
    params = {
        "warehouse_id": "wh-1",
        "volume": VOLUME,
        "schema": SCHEMA,
    }
    params.update(kwargs)
    return document_extract_tool(**params)


def test_registry_maps_document_extract():
    import apx_agent._tool_config as mod
    from apx_agent.document_extract import document_extract_tool as factory

    registry = mod._registry()
    assert "document_extract" in registry
    assert registry["document_extract"] is factory


def test_public_export():
    import apx_agent

    assert "document_extract_tool" in apx_agent.__all__
    assert apx_agent.document_extract_tool is document_extract_tool


def test_load_config_builds_named_warehouse_tool():
    tools = load_config_tools([
        {
            "type": "document_extract",
            "warehouse_id": "wh-prod",
            "volume": VOLUME,
            "schema": SCHEMA,
            "name": "extract_contract",
        }
    ])
    assert len(tools) == 1
    assert tools[0].__name__ == "extract_contract"
    assert ResourceSpec("sql_warehouse", "wh-prod") in get_resources(tools[0])


@pytest.mark.parametrize(
    "table, match",
    [
        ({"type": "document_extract", "volume": VOLUME, "schema": SCHEMA}, "warehouse_id"),
        ({"type": "document_extract", "warehouse_id": "wh-1", "schema": SCHEMA}, "volume"),
        ({"type": "document_extract", "warehouse_id": "wh-1", "volume": VOLUME}, "schema"),
        (
            {
                "type": "document_extract",
                "warehouse_id": "wh-1",
                "volume": "not-three-parts",
                "schema": SCHEMA,
            },
            "catalog.schema.volume",
        ),
        (
            {
                "type": "document_extract",
                "warehouse_id": "wh-1",
                "volume": "main.contracts.raw-contracts",
                "schema": SCHEMA,
            },
            "catalog.schema.volume",
        ),
    ],
)
def test_missing_or_invalid_config_raises(table, match):
    with pytest.raises(ToolConfigError, match=match):
        load_config_tools([table])


def test_schema_file_missing_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ToolConfigError, match="schema file not found"):
        document_extract_tool(
            warehouse_id="wh-1",
            volume=VOLUME,
            schema="schemas/contract.json",
        )


def test_schema_file_invalid_json_raises(tmp_path, monkeypatch):
    schema_path = tmp_path / "schemas" / "contract.json"
    schema_path.parent.mkdir()
    schema_path.write_text("{not-json")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ToolConfigError, match="valid JSON"):
        document_extract_tool(
            warehouse_id="wh-1",
            volume=VOLUME,
            schema="schemas/contract.json",
        )


def test_schema_file_non_object_raises(tmp_path, monkeypatch):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text("[1, 2]")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ToolConfigError, match="JSON object"):
        document_extract_tool(
            warehouse_id="wh-1",
            volume=VOLUME,
            schema="schema.json",
        )


@pytest.mark.parametrize(
    "user_path, expected",
    [
        ("foo/bar.pdf", f"{VOLUME_ROOT}/foo/bar.pdf"),
        (f"{VOLUME_ROOT}/foo/bar.pdf", f"{VOLUME_ROOT}/foo/bar.pdf"),
        ("./nested/doc.pdf", f"{VOLUME_ROOT}/nested/doc.pdf"),
    ],
)
def test_bind_accepts_in_volume_paths(user_path, expected):
    assert bind_volume_path(VOLUME_ROOT, user_path) == expected


@pytest.mark.parametrize(
    "user_path",
    [
        "",
        "   ",
        "../escape.pdf",
        "foo/../../other/x.pdf",
        "/Volumes/other/vol/x.pdf",
        f"{VOLUME_ROOT}/../other/x.pdf",
        VOLUME_ROOT,
        "foo\x00.pdf",
    ],
)
def test_bind_rejects_escape_and_empty(user_path):
    with pytest.raises(ToolError):
        bind_volume_path(VOLUME_ROOT, user_path)


@pytest.mark.asyncio
async def test_call_binds_path_and_schema_not_interpolated(monkeypatch):
    captured: dict = {}

    def fake_run_sql(ws, sql, *, warehouse_id=None, parameters=None):
        captured.update(sql=sql, warehouse_id=warehouse_id, parameters=parameters)
        return [{"extracted": {"title": "MSA"}, "citations": [{"page": 1}]}]

    monkeypatch.setattr("apx_agent.document_extract.run_sql", fake_run_sql)
    tool = _tool()
    result = await tool(path="foo/bar.pdf", ws=MagicMock())

    assert result["path"] == f"{VOLUME_ROOT}/foo/bar.pdf"
    assert result["extracted"] == {"title": "MSA"}
    assert result["citations"] == [{"page": 1}]
    assert captured["warehouse_id"] == "wh-1"
    assert ":volume_path" in captured["sql"]
    assert ":extract_schema" in captured["sql"]
    assert "foo/bar.pdf" not in captured["sql"]
    assert "title" not in captured["sql"]
    names = {p["name"]: p for p in captured["parameters"]}
    assert names["volume_path"]["value"] == f"{VOLUME_ROOT}/foo/bar.pdf"
    assert json.loads(names["extract_schema"]["value"]) == SCHEMA


@pytest.mark.asyncio
async def test_schema_file_is_loaded_at_factory_time(tmp_path, monkeypatch):
    schema_path = tmp_path / "schemas" / "contract.json"
    schema_path.parent.mkdir()
    schema_path.write_text(json.dumps(SCHEMA))
    monkeypatch.chdir(tmp_path)
    captured: dict = {}

    def fake_run_sql(ws, sql, *, warehouse_id=None, parameters=None):
        captured["parameters"] = parameters
        return [{"extracted": {"title": "ok"}}]

    monkeypatch.setattr("apx_agent.document_extract.run_sql", fake_run_sql)
    tool = document_extract_tool(
        warehouse_id="wh-1",
        volume=VOLUME,
        schema="schemas/contract.json",
    )
    schema_path.write_text(json.dumps({"type": "object", "properties": {"changed": {}}}))
    await tool(path="a.pdf", ws=MagicMock())
    bound = json.loads(captured["parameters"][1]["value"])
    assert bound == SCHEMA


@pytest.mark.asyncio
async def test_escape_path_does_not_call_warehouse(monkeypatch):
    called = []
    monkeypatch.setattr(
        "apx_agent.document_extract.run_sql",
        lambda *a, **k: called.append(1) or [],
    )
    tool = _tool()
    with pytest.raises(ToolError, match="outside the declared volume"):
        await tool(path="../escape.pdf", ws=MagicMock())
    assert called == []


@pytest.mark.asyncio
async def test_warehouse_failure_is_toolerror_no_fallback(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("FUNCTION_NOT_FOUND: ai_parse_document")

    monkeypatch.setattr("apx_agent.document_extract.run_sql", boom)
    tool = _tool()
    with pytest.raises(ToolError, match="ai_parse_document") as excinfo:
        await tool(path="foo.pdf", ws=MagicMock())
    assert "PyMuPDF" not in str(excinfo.value)
    assert excinfo.value.__cause__ is not None


def test_attaches_only_sql_warehouse_resource():
    specs = get_resources(_tool(warehouse_id="wh-prod"))
    assert specs == [ResourceSpec("sql_warehouse", "wh-prod")]
