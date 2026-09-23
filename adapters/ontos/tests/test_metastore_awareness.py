"""Tests for metastore-awareness in the Ontos governance adapter.

Verifies that metastore_id support is properly wired into the adapter's
provider protocol and GovernanceProvider implementation.

Companion-side metastore-awareness tests for the engine (MCP tools,
guardrails client, Genie SQL templates) live in the governance-monitoring
repo's tests/unit/test_metastore_awareness.py.
"""

import sys
from pathlib import Path

ADAPTER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ADAPTER_ROOT / "src"))


class TestOntosMetastoreInfo:
    def test_metastore_info_model(self):
        from ontos_governance.models import MetastoreInfo

        info = MetastoreInfo(metastore_id="ms-abc")
        assert info.metastore_id == "ms-abc"
        assert info.latest_scan is None
        assert info.resource_count == 0
        assert info.last_scanned is None

    def test_metastore_info_with_all_fields(self):
        from ontos_governance.models import MetastoreInfo

        info = MetastoreInfo(
            metastore_id="ms-abc",
            latest_scan="scan-42",
            resource_count=100,
            last_scanned="2026-04-01T12:00:00",
        )
        assert info.resource_count == 100
        assert info.latest_scan == "scan-42"


class TestOntosProviderProtocol:
    def test_protocol_has_list_metastores(self):
        import inspect

        from ontos_governance.provider import GovernanceProvider

        assert hasattr(GovernanceProvider, "list_metastores")
        sig = inspect.signature(GovernanceProvider.list_metastores)
        assert "self" in sig.parameters

    def test_protocol_has_set_active_metastore(self):
        import inspect

        from ontos_governance.provider import GovernanceProvider

        assert hasattr(GovernanceProvider, "set_active_metastore")
        sig = inspect.signature(GovernanceProvider.set_active_metastore)
        assert "metastore_id" in sig.parameters

    def test_violations_summary_has_metastore_param(self):
        import inspect

        from ontos_governance.provider import GovernanceProvider

        sig = inspect.signature(GovernanceProvider.violations_summary)
        assert "metastore_id" in sig.parameters
        assert sig.parameters["metastore_id"].default is None

    def test_list_violations_has_metastore_param(self):
        import inspect

        from ontos_governance.provider import GovernanceProvider

        sig = inspect.signature(GovernanceProvider.list_violations)
        assert "metastore_id" in sig.parameters

    def test_list_resources_has_metastore_param(self):
        import inspect

        from ontos_governance.provider import GovernanceProvider

        sig = inspect.signature(GovernanceProvider.list_resources)
        assert "metastore_id" in sig.parameters


class TestGovernanceProviderMetastore:
    def test_set_active_metastore(self):
        from ontos_governance.providers.governance import GovernanceProvider

        provider = GovernanceProvider(
            server_hostname="host", http_path="/path", access_token="tok"
        )
        assert provider._active_metastore is None

        provider.set_active_metastore("ms-abc")
        assert provider._active_metastore == "ms-abc"

        provider.set_active_metastore(None)
        assert provider._active_metastore is None

    def test_resolve_metastore_priority(self):
        from ontos_governance.providers.governance import GovernanceProvider

        provider = GovernanceProvider(
            server_hostname="host", http_path="/path", access_token="tok"
        )

        assert provider._resolve_metastore() is None

        provider.set_active_metastore("ms-active")
        assert provider._resolve_metastore() == "ms-active"

        assert provider._resolve_metastore("ms-param") == "ms-param"

    def test_metastore_clause_generation(self):
        from ontos_governance.providers.governance import GovernanceProvider

        provider = GovernanceProvider(
            server_hostname="host", http_path="/path", access_token="tok"
        )

        assert provider._metastore_clause() == ""

        provider.set_active_metastore("ms-abc")
        clause = provider._metastore_clause()
        assert "metastore_id = 'ms-abc'" in clause
        assert clause.startswith("AND")

        clause_where = provider._metastore_clause(prefix="WHERE")
        assert clause_where.startswith("WHERE")
