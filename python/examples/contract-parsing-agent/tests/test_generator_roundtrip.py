"""Round-trip: synthetic contract -> extraction -> match ground truth.

Hits the real FM API. Run with:
    uv run pytest tests/test_generator_roundtrip.py -m integration -v
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from databricks.sdk import WorkspaceClient

from contract_parsing_agent.backend.config import load_settings
from contract_parsing_agent.backend.extraction import extract
from scripts.generate_synthetic_contracts import generate


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ws() -> WorkspaceClient:
    return WorkspaceClient()


@pytest.fixture(scope="module")
def settings(tmp_path_factory):
    cfg_path = Path(__file__).resolve().parents[1] / "agent.config.yaml"
    return load_settings(cfg_path)


def test_roundtrip_three_contracts(ws, settings, tmp_path: Path) -> None:
    rows = generate(tmp_path, n=3, seed=42)
    fields_to_check = [
        "counterparty", "contract_type", "term_years",
        "pricing_model", "auto_renewal",
    ]
    correct = 0
    total = 0
    for gt in rows:
        pdf = tmp_path / f"{gt['contract_id']}.pdf"
        result = extract(pdf, settings.extraction_schema, settings.model, ws)
        for f in fields_to_check:
            total += 1
            actual = gt[f]
            predicted = result.get(f)
            if isinstance(actual, float) and isinstance(predicted, (int, float)):
                if abs(predicted - actual) < 0.5:
                    correct += 1
            elif predicted == actual:
                correct += 1
    accuracy = correct / total
    assert accuracy >= 0.8, (
        f"round-trip accuracy {accuracy:.2%} below 80% threshold "
        f"({correct}/{total} fields). The eval premise is broken — fix the prompt or schema."
    )
