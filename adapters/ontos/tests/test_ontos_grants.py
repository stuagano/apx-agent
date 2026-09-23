"""Unit tests for grant models, provider methods, and grants router.

Tests the new Grant, GrantSummary, GrantFilters models and the
grants router endpoint shapes.

Run with: pytest adapters/ontos/tests/test_ontos_grants.py -v
"""

import sys
from pathlib import Path

# Ensure the adapter package is importable
ADAPTER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ADAPTER_ROOT / "src"))

import pytest
from ontos_governance.models import Grant, GrantFilters, GrantSummary
from pydantic import ValidationError

# ── Grant model ──────────────────────────────────────────────────────────────


class TestGrantModel:
    def test_valid_grant(self):
        g = Grant(
            resource_id="grant:abc123",
            securable_type="TABLE",
            securable_full_name="catalog.schema.table1",
            grantee="data_team",
            privilege="SELECT",
            grantor="admin_user",
            inherited_from="catalog.schema",
        )
        assert g.resource_id == "grant:abc123"
        assert g.privilege == "SELECT"
        assert g.securable_full_name == "catalog.schema.table1"

    def test_grant_requires_all_fields(self):
        with pytest.raises(ValidationError):
            Grant(
                resource_id="grant:abc123",
                securable_type="TABLE",
                # Missing required fields
            )

    def test_grant_serialization(self):
        g = Grant(
            resource_id="grant:abc123",
            securable_type="TABLE",
            securable_full_name="catalog.schema.table1",
            grantee="data_team",
            privilege="ALL PRIVILEGES",
            grantor="admin",
            inherited_from="",
        )
        d = g.model_dump()
        assert d["privilege"] == "ALL PRIVILEGES"
        assert d["inherited_from"] == ""


# ── GrantSummary model ───────────────────────────────────────────────────────


class TestGrantSummaryModel:
    def test_valid_summary(self):
        s = GrantSummary(
            resource_id="catalog.schema.table1",
            total_grants=5,
            grants_by_privilege={"SELECT": 3, "ALL PRIVILEGES": 2},
            overprivileged_count=2,
            direct_user_grant_count=1,
        )
        assert s.total_grants == 5
        assert s.grants_by_privilege["SELECT"] == 3
        assert s.overprivileged_count == 2

    def test_summary_empty_grants(self):
        s = GrantSummary(
            resource_id="catalog.schema.table1",
            total_grants=0,
            grants_by_privilege={},
            overprivileged_count=0,
            direct_user_grant_count=0,
        )
        assert s.total_grants == 0
        assert s.grants_by_privilege == {}


# ── GrantFilters model ───────────────────────────────────────────────────────


class TestGrantFiltersModel:
    def test_defaults_all_none(self):
        f = GrantFilters()
        assert f.resource_id is None
        assert f.grantee is None
        assert f.privilege is None
        assert f.securable_type is None

    def test_with_filters(self):
        f = GrantFilters(
            resource_id="catalog.schema.table1",
            grantee="data_team",
            privilege="SELECT",
            securable_type="TABLE",
        )
        assert f.resource_id == "catalog.schema.table1"
        assert f.grantee == "data_team"

    def test_partial_filters(self):
        f = GrantFilters(grantee="admin")
        assert f.grantee == "admin"
        assert f.resource_id is None
        assert f.privilege is None


# ── Grants router shape ─────────────────────────────────────────────────────


class TestGrantsRouter:
    """Verify the router module can be imported and has expected endpoints."""

    def test_router_importable(self):
        from ontos_governance.routers.grants import router
        assert router is not None

    def test_router_has_list_grants(self):
        from ontos_governance.routers.grants import router
        paths = [r.path for r in router.routes]
        assert "/grants" in paths

    def test_router_has_grant_summary(self):
        from ontos_governance.routers.grants import router
        paths = [r.path for r in router.routes]
        assert "/grants/summary/{resource_id:path}" in paths

    def test_router_registered_in_main(self):
        """Verify the grants router is included in the root router."""
        from ontos_governance.router import root_router

        def _paths(routes):
            found = []
            for route in routes:
                path = getattr(route, "path", None)
                if path:
                    found.append(path)
                # Starlette 1.6+ keeps include_router as _IncludedRouter.
                orig = getattr(route, "original_router", None)
                if orig is not None:
                    found.extend(_paths(orig.routes))
            return found

        assert "/grants" in _paths(root_router.routes)
