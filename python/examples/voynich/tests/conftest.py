"""
conftest.py — shared pytest fixtures for voynich-agents tests.

Available fixtures:
  loop_config      → LoopConfig with test defaults
  hypothesis       → a single Hypothesis with realistic field values
  population       → list[Hypothesis] of 10, spanning cipher types and languages
  mock_ws          → MagicMock WorkspaceClient with pre-wired SQL responses
  mock_store       → PopulationStore wired to mock_ws, Spark disabled
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Generator
from unittest.mock import MagicMock

import pytest

from apx_agent.workflow import (
    Hypothesis,
    LoopConfig,
    CipherType,
    SourceLanguage,
)
from apx_agent.workflow.population_store import PopulationStore


# ---------------------------------------------------------------------------
# LoopConfig
# ---------------------------------------------------------------------------

@pytest.fixture
def loop_config() -> LoopConfig:
    return LoopConfig(
        population_table  = "voynich.evolution.population",
        fitness_agents    = ["http://historian.test", "http://critic.test"],
        mutation_agent    = "http://decipherer.test",
        judge_agent       = "http://judge.test",
        review_table      = "voynich.evolution.review_queue",
        warehouse_id      = "test-warehouse-id",
        population_size   = 20,
        mutation_batch    = 5,
        max_generations   = 10,
        escalation_threshold = 0.90,
    )


# ---------------------------------------------------------------------------
# Hypothesis factories
# ---------------------------------------------------------------------------

@pytest.fixture
def hypothesis() -> Hypothesis:
    return Hypothesis(
        id              = "test01",
        generation      = 1,
        parent_id       = None,
        cipher_type     = CipherType.SUBSTITUTION,
        source_language = SourceLanguage.LATIN,
        symbol_map      = {"o": "a", "a": "e", "i": "i", "n": "n", "s": "s"},
        null_chars      = ["q"],
        transformation_rules = [],
        fitness_statistical  = 0.70,
        fitness_perplexity   = 0.65,
        fitness_semantic     = 0.80,
        fitness_consistency  = 0.60,
        fitness_adversarial  = 0.00,
        agent_eval_historian = 0.75,
        agent_eval_critic    = 0.70,
        decoded_sample       = "the root of this plant boiled in water cures pain",
        mlflow_run_id        = "abc123mlflow",
    )


@pytest.fixture
def population() -> list[Hypothesis]:
    """10 hypotheses spanning all cipher types and common languages."""
    configs = [
        (CipherType.SUBSTITUTION,    SourceLanguage.LATIN,   0.80, 0.75, 0.85, 0.70),
        (CipherType.SUBSTITUTION,    SourceLanguage.HEBREW,  0.50, 0.45, 0.55, 0.40),
        (CipherType.POLYALPHABETIC,  SourceLanguage.LATIN,   0.65, 0.60, 0.70, 0.55),
        (CipherType.POLYALPHABETIC,  SourceLanguage.ARABIC,  0.45, 0.40, 0.50, 0.35),
        (CipherType.NULL_BEARING,    SourceLanguage.LATIN,   0.72, 0.68, 0.78, 0.62),
        (CipherType.NULL_BEARING,    SourceLanguage.ITALIAN, 0.60, 0.55, 0.65, 0.50),
        (CipherType.TRANSPOSITION,   SourceLanguage.LATIN,   0.55, 0.50, 0.60, 0.45),
        (CipherType.COMPOSITE,       SourceLanguage.LATIN,   0.85, 0.80, 0.90, 0.75),
        (CipherType.COMPOSITE,       SourceLanguage.HEBREW,  0.40, 0.35, 0.45, 0.30),
        (CipherType.STEGANOGRAPHIC,  SourceLanguage.LATIN,   0.30, 0.25, 0.35, 0.20),
    ]
    return [
        Hypothesis(
            id              = f"pop{i:02d}",
            generation      = 1,
            cipher_type     = ct,
            source_language = sl,
            symbol_map      = {"o": "a", "a": "e"},
            null_chars      = ["q"] if "null" in ct else [],
            fitness_statistical = fs,
            fitness_perplexity  = fp,
            fitness_semantic    = fse,
            fitness_consistency = fc,
        )
        for i, (ct, sl, fs, fp, fse, fc) in enumerate(configs)
    ]


# ---------------------------------------------------------------------------
# Mock WorkspaceClient + PopulationStore
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_ws() -> MagicMock:
    ws = MagicMock()
    _wire_empty_sql(ws)
    return ws


def _wire_empty_sql(ws: MagicMock) -> None:
    """Configure mock WorkspaceClient to return empty SQL results."""
    from databricks.sdk.service.sql import StatementState
    resp = MagicMock()
    resp.status.state  = StatementState.SUCCEEDED
    resp.result        = None
    ws.statement_execution.execute_statement.return_value = resp


def _wire_sql_rows(ws: MagicMock, rows: list[dict]) -> None:
    """Configure mock WorkspaceClient to return specific rows."""
    from databricks.sdk.service.sql import StatementState
    resp = MagicMock()
    resp.status.state = StatementState.SUCCEEDED
    if rows:
        cols = list(rows[0].keys())
        resp.manifest.schema.columns = [SimpleNamespace(name=c) for c in cols]
        resp.result.data_array = [[r.get(c) for c in cols] for r in rows]
    else:
        resp.result = None
    ws.statement_execution.execute_statement.return_value = resp


@pytest.fixture
def mock_store(loop_config: LoopConfig, mock_ws: MagicMock) -> PopulationStore:
    """PopulationStore wired to mock_ws with Spark disabled."""
    store = PopulationStore(mock_ws, loop_config)
    store._spark = None   # force SQL fallback
    return store


# ---------------------------------------------------------------------------
# Hypothesis row helpers (for wiring SQL mock responses)
# ---------------------------------------------------------------------------

def hypothesis_to_row(h: Hypothesis) -> dict:
    """Convert Hypothesis to the flat dict that _sql_exec returns."""
    d = h.to_dict()
    d["flagged_for_review"] = h.flagged_for_review
    return d


# Export helpers for use in test files
__all__ = ["_wire_sql_rows", "hypothesis_to_row"]
