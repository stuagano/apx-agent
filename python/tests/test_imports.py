"""Smoke tests for cross-package import contracts.

These tests verify that the optional-extras pin floors in
``python/pyproject.toml`` keep the resolver away from versions that break
known-required imports. They run only when the relevant extra is installed
locally; CI installs ``apx-agent[langgraph]`` and other extras to exercise
them.
"""

from __future__ import annotations

from pathlib import Path
import tomllib

import pytest


def test_mlflow_extras_include_databricks_uc_dependencies() -> None:
    """The local eval/align extras must match deployed Apps' MLflow contract."""
    pyproject = tomllib.loads(
        Path(__file__).parents[1].joinpath("pyproject.toml").read_text()
    )
    extras = pyproject["project"]["optional-dependencies"]
    assert "mlflow[databricks]>=3.14,<3.15" in extras["eval"]
    assert "mlflow[databricks]>=3.14,<3.15" in extras["align"]


def test_langgraph_runtime_exposes_execution_info() -> None:
    """``langchain`` 1.2+ imports ``ExecutionInfo`` from ``langgraph.runtime``.

    The symbol was added in ``langgraph>=1.1.0``. With a looser pin
    (``>=1.0.0``) the resolver can land on 1.0.10, which causes an
    ``ImportError`` the moment ``langchain.agents.create_agent`` is touched
    inside a deployed App. The floor in ``[project.optional-dependencies]``
    ``langgraph`` keeps that from happening — this test catches the
    regression at install time.
    """
    pytest.importorskip("langgraph")
    from langgraph.runtime import ExecutionInfo  # noqa: F401

    # Trivial reference so the import isn't elided by a future linter.
    assert ExecutionInfo is not None
