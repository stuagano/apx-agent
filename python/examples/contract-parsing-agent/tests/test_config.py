from pathlib import Path
import textwrap

import pytest
import yaml

from config import Settings, load_settings


def test_bundle_stages_repo_local_tools_core_wheel() -> None:
    bundle = yaml.safe_load((Path(__file__).parents[1] / "databricks.yml").read_text())
    build = bundle["artifacts"]["default"]["build"]

    assert "rm -f .build/*.whl .build/requirements.txt" in build
    assert "uv build --wheel -o .build/ ../databricks-tools-core" in build
    assert "uv build --wheel -o .build/ ../.." in build
    assert "databricks_tools_core-*.whl >> .build/requirements.txt" in build
    assert "apx_agent-*.whl >> .build/requirements.txt" in build


def test_load_settings_from_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "agent.config.yaml"
    cfg.write_text(textwrap.dedent("""
        catalog: test_cat
        schema: silver
        tables:
          primary: contracts
          ground_truth: contracts_ground_truth
        volumes:
          raw: /Volumes/test_cat/silver/raw_contracts
          uploads: /Volumes/test_cat/silver/uploads
        model: databricks-claude-sonnet-4-6
        system_prompt: "You are a contracts analyst."
        demo_questions:
          - "Which contracts have auto-renewal?"
        sub_agents:
          - https://data-inspector.example/com
        extraction_schema:
          type: object
          properties:
            counterparty:
              type: string
          required: [counterparty]
    """))
    s = load_settings(cfg)
    assert isinstance(s, Settings)
    assert s.catalog == "test_cat"
    assert s.tables.primary == "contracts"
    assert s.qualified_table("primary") == "test_cat.silver.contracts"
    assert s.qualified_table("ground_truth") == "test_cat.silver.contracts_ground_truth"
    assert "counterparty" in s.extraction_schema["properties"]
    assert s.model == "databricks-claude-sonnet-4-6"


def test_load_settings_missing_required_field(tmp_path: Path) -> None:
    cfg = tmp_path / "agent.config.yaml"
    cfg.write_text("catalog: only_cat\n")
    with pytest.raises(Exception):
        load_settings(cfg)
