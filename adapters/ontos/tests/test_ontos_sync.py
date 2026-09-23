"""Unit tests for Ontos IRI parameterization.

Tests the IRI resolution fallback chain without calling any external APIs.

Run with: pytest adapters/ontos/tests/test_ontos_sync.py -v
"""
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock heavy dependencies BEFORE any imports touch the adapter package.
# The package __init__.py chains through router → providers → databricks.sql,
# so we stub out every databricks sub-module and pyspark up front.
# ---------------------------------------------------------------------------

def _make_module(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m

_pyspark       = _make_module("pyspark")
_pyspark_sql   = _make_module("pyspark.sql", SparkSession=MagicMock)
_databricks    = _make_module("databricks")
_db_sdk        = _make_module("databricks.sdk", WorkspaceClient=MagicMock)
_db_sql        = _make_module("databricks.sql", connect=MagicMock)

for _name, _mod in [
    ("pyspark",        _pyspark),
    ("pyspark.sql",    _pyspark_sql),
    ("databricks",     _databricks),
    ("databricks.sdk", _db_sdk),
    ("databricks.sql", _db_sql),
]:
    sys.modules.setdefault(_name, _mod)

# Add the adapter to path BEFORE stubbing the package so that the real
# package directory is discoverable.
ADAPTER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ADAPTER_ROOT / "src"))

# Stub the ontos_governance package itself so that importing
# ontos_governance.ontos_sync does NOT execute __init__.py (which would
# trigger the full router/provider import chain).
_pkg_path = str(ADAPTER_ROOT / "src" / "ontos_governance")
_wg_pkg = _make_module("ontos_governance")
_wg_pkg.__path__ = [_pkg_path]   # marks it as a package so sub-modules resolve
_wg_pkg.__package__ = "ontos_governance"
sys.modules["ontos_governance"] = _wg_pkg  # replace any real entry

# Import the module directly (bypasses __init__.py)
import importlib

ontos_sync = importlib.import_module("ontos_governance.ontos_sync")
resolve_ontology_base_iri = ontos_sync.resolve_ontology_base_iri


class TestResolveOntologyBaseIri:

    def test_explicit_parameter_wins(self):
        result = resolve_ontology_base_iri(
            ontology_base_iri="https://custom.com/onto/",
            workspace_host="https://workspace.cloud.databricks.com",
        )
        assert result == "https://custom.com/onto/"

    def test_env_var_fallback(self):
        with patch.dict(os.environ, {"GOVERNANCE_ONTOLOGY_BASE_IRI": "https://env.com/onto/"}):
            result = resolve_ontology_base_iri(
                ontology_base_iri=None,
                workspace_host="https://workspace.cloud.databricks.com",
            )
        assert result == "https://env.com/onto/"

    def test_workspace_host_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("GOVERNANCE_ONTOLOGY_BASE_IRI", None)
            result = resolve_ontology_base_iri(
                ontology_base_iri=None,
                workspace_host="https://myworkspace.cloud.databricks.com",
            )
        assert result == "https://myworkspace.cloud.databricks.com/ontology/governance/class/"

    def test_trailing_slash_enforced(self):
        result = resolve_ontology_base_iri(
            ontology_base_iri="https://custom.com/onto",
            workspace_host="https://workspace.cloud.databricks.com",
        )
        assert result.endswith("/")

    def test_env_var_trailing_slash_enforced(self):
        with patch.dict(os.environ, {"GOVERNANCE_ONTOLOGY_BASE_IRI": "https://env.com/onto"}):
            result = resolve_ontology_base_iri(
                ontology_base_iri=None,
                workspace_host="https://workspace.cloud.databricks.com",
            )
        assert result.endswith("/")
