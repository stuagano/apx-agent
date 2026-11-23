"""Verify docs/coworkers specs load, and stay aligned with the gallery.

The packaged gallery (`src/apx_agent/coworker_gallery/`) is the scaffold
skeleton (`tools: []`). The copies under `docs/coworkers/` are the worked
examples and declare one governed `sql` tool. Anything else that drifts
between a pair fails `test_gallery_and_docs_coworker_specs_agree_except_tools`.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from apx_agent._models import AgentConfig
from apx_agent._tool_config import load_config_tools
from apx_agent._resources import get_resources
from apx_agent._yaml_spec import load_spec


COWORKERS_DIR = Path(__file__).parent.parent.parent / "docs" / "coworkers"
GALLERY_DIR = Path(__file__).parent.parent / "src" / "apx_agent" / "coworker_gallery"
YAML_FILES = list(COWORKERS_DIR.glob("*.yaml"))
DOCS_ONLY = "financial-reporting.yaml"
_GENIE_MARK = "# - type: genie"
_GOVERNED_MARK = "# The governed data tool."
_RETIRED = ("lakebase", "responses_agent")


@pytest.mark.parametrize("yaml_file", YAML_FILES, ids=lambda f: f.stem)
def test_reference_yaml_loads(yaml_file, monkeypatch):
    # Substitute env vars so $CATALOG etc. don't block validation
    monkeypatch.setenv("CATALOG", "main")
    monkeypatch.setenv("SCHEMA", "test_schema")
    monkeypatch.setenv("WAREHOUSE_ID", "test_warehouse")
    monkeypatch.setenv("GENIE_SPACE_ID", "test_genie_space")
    monkeypatch.setenv("LAKEBASE_HOST", "test-lakebase.example.com")

    config = load_spec(yaml_file)
    assert config.name, f"Expected non-empty name from {yaml_file.name}"
    assert config.template is not None, f"Expected template block in {yaml_file.name}"
    assert config.template.get("name") == "coworker", f"Expected template.name=coworker in {yaml_file.name}"

    # Any declared tools must actually build through the real factory path —
    # a `tools:` block that names a governed resource has to produce it, not
    # just parse. (Claim-vs-reality: prove the wiring, don't trust the YAML.)
    if config.tools:
        built = load_config_tools(config.tools)
        assert len(built) >= len(config.tools), (
            f"{yaml_file.name}: {len(config.tools)} tools declared but only "
            f"{len(built)} built"
        )
        for decl, tool in zip(config.tools, built):
            wid = decl.get("warehouse_id")
            if wid:
                specs = [(r.kind, r.identifier) for r in get_resources(tool)]
                assert ("sql_warehouse", wid) in specs, (
                    f"{yaml_file.name}: tool {decl.get('type')} declares "
                    f"warehouse {wid} but it is not governed as a resource: {specs}"
                )


@dataclass(frozen=True)
class _SpecParts:
    prefix: str
    tools_region: str
    suffix: str


def _split_spec(text: str, name: str) -> _SpecParts:
    """Split a coworker spec into the shared text and the tools block.

    The allowed difference between a gallery skeleton and its docs copy is
    the region from the governed-tool comment (or ``tools:``) through the
    line before the commented Genie block.
    """
    genie_at = text.find(_GENIE_MARK)
    assert genie_at >= 0, f"{name} has no commented Genie block"
    assert text.find(_GENIE_MARK, genie_at + len(_GENIE_MARK)) < 0, (
        f"{name} has more than one Genie block"
    )
    head = text[:genie_at]
    tools_at = head.rfind("\ntools:")
    assert tools_at >= 0, f"{name} has no tools block before the Genie comment"
    governed_at = head.rfind(_GOVERNED_MARK)
    start = governed_at if governed_at != -1 else tools_at + 1
    return _SpecParts(prefix=head[:start], tools_region=head[start:], suffix=text[genie_at:])


def _without_tools(data: dict[str, Any]) -> dict[str, Any]:
    rest = dict(data)
    del rest["tools"]
    return rest


def _assert_no_retired_runtime(path: Path) -> None:
    lowered = path.read_text().lower()
    for token in _RETIRED:
        assert token not in lowered, f"{path.name} still mentions {token}"


def _assert_managed_durable(config: AgentConfig, name: str) -> None:
    assert config.target == "durable_agent_server", name
    assert config.memory is not None and config.memory.type == "managed", name
    assert config.session is not None and config.session.type == "managed", name


def _fill_spec_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATALOG", "main")
    monkeypatch.setenv("SCHEMA", "test_schema")
    monkeypatch.setenv("WAREHOUSE_ID", "test_warehouse")
    monkeypatch.setenv("GENIE_SPACE_ID", "test_genie_space")


def test_gallery_and_docs_coworker_specs_agree_except_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gallery scaffolds keep ``tools: []``; docs copies add one ``sql`` tool.

    Session and memory stay ``type: managed`` on both sides, and neither
    side may bring back Lakebase or ``responses_agent``. ``financial-reporting.yaml``
    is the one docs-only spec.
    """
    _fill_spec_env(monkeypatch)
    gallery = sorted(GALLERY_DIR.glob("*.yaml"))
    docs = {path.name: path for path in COWORKERS_DIR.glob("*.yaml")}
    gallery_names = {path.name for path in gallery}
    assert gallery_names, "coworker gallery is empty"
    assert gallery_names <= set(docs), sorted(gallery_names - set(docs))
    assert set(docs) - gallery_names == {DOCS_ONLY}

    for path in gallery:
        docs_path = docs[path.name]
        gallery_text = path.read_text()
        docs_text = docs_path.read_text()
        gallery_parts = _split_spec(gallery_text, path.name)
        docs_parts = _split_spec(docs_text, f"docs/{path.name}")
        assert gallery_parts.prefix == docs_parts.prefix, path.name
        assert gallery_parts.suffix == docs_parts.suffix, path.name
        assert gallery_parts.tools_region.startswith("tools: []\n"), path.name
        assert "type: sql" not in gallery_parts.tools_region, path.name
        assert "type: sql" in docs_parts.tools_region, path.name
        assert "warehouse_id: $WAREHOUSE_ID" in docs_parts.tools_region, path.name

        gallery_raw = yaml.safe_load(gallery_text)
        docs_raw = yaml.safe_load(docs_text)
        assert isinstance(gallery_raw, dict) and isinstance(docs_raw, dict)
        assert _without_tools(gallery_raw) == _without_tools(docs_raw), path.name
        assert gallery_raw["tools"] == [], path.name
        docs_tools = docs_raw["tools"]
        assert isinstance(docs_tools, list) and len(docs_tools) == 1, path.name
        tool = docs_tools[0]
        assert tool["type"] == "sql", path.name
        assert tool["warehouse_id"] == "$WAREHOUSE_ID", path.name
        assert isinstance(tool["name"], str) and tool["name"], path.name
        assert isinstance(tool["description"], str) and tool["description"], path.name

        for spec_path in (path, docs_path):
            _assert_no_retired_runtime(spec_path)
            _assert_managed_durable(load_spec(spec_path), spec_path.name)

    reporting = docs[DOCS_ONLY]
    _assert_no_retired_runtime(reporting)
    reporting_raw = yaml.safe_load(reporting.read_text())
    assert isinstance(reporting_raw, dict)
    reporting_tools = reporting_raw["tools"]
    assert isinstance(reporting_tools, list) and len(reporting_tools) == 1
    assert reporting_tools[0]["type"] == "sql"
    assert reporting_tools[0]["warehouse_id"] == "$WAREHOUSE_ID"
    reporting_config = load_spec(reporting)
    _assert_managed_durable(reporting_config, DOCS_ONLY)
    assert len(reporting_config.tools) == 1
    assert reporting_config.tools[0]["type"] == "sql"


def test_coworker_readme_records_the_tool_split() -> None:
    readme = (COWORKERS_DIR / "README.md").read_text()
    assert "coworker_gallery" in readme
    assert "tools: []" in readme
    assert "sql" in readme
    assert "financial-reporting.yaml" in readme
    assert "type: managed" in readme
