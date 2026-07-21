"""Verify that all docs/coworkers/*.yaml reference specs load without error."""
import os
from pathlib import Path
import pytest
from apx_agent._tool_config import load_config_tools
from apx_agent._resources import get_resources
from apx_agent._yaml_spec import load_spec


COWORKERS_DIR = Path(__file__).parent.parent.parent / "docs" / "coworkers"
YAML_FILES = list(COWORKERS_DIR.glob("*.yaml"))


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
