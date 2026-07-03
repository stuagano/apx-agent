"""Tests for _doctor.py — the apx environment diagnostic layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import apx_agent._doctor as doctor
from apx_agent._doctor import Check, Status, run_checks


def test_check_is_frozen_dataclass():
    c = Check(name="X", status=Status.OK, detail="fine", fix=None)
    assert c.name == "X"
    assert c.status is Status.OK
    assert c.fix is None


def test_run_checks_returns_ordered_groups(tmp_path: Path):
    groups = run_checks(tmp_path, online=False)
    names = [g for g, _ in groups]
    assert names == ["Environment", "Authentication", "Project"]
    for _group, checks in groups:
        assert all(isinstance(c, Check) for c in checks)


def test_python_version_ok(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 12, 2, "final", 0))
    c = doctor.check_python_version()
    assert c.status is doctor.Status.OK
    assert "3.12.2" in c.detail


def test_python_version_too_old(monkeypatch):
    monkeypatch.setattr(doctor.sys, "version_info", (3, 10, 9, "final", 0))
    c = doctor.check_python_version()
    assert c.status is doctor.Status.FAIL
    assert "3.11" in c.fix


def test_uv_present(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/uv")
    c = doctor.check_uv()
    assert c.status is doctor.Status.OK


def test_uv_missing_is_warn(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_uv()
    assert c.status is doctor.Status.WARN
    assert c.fix is not None


def _clear_index_env(monkeypatch):
    for var in doctor._INDEX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_pypi_index_ok_clean_env(monkeypatch, tmp_path: Path):
    _clear_index_env(monkeypatch)
    c = doctor.check_pypi_index(tmp_path)
    assert c.status is doctor.Status.OK
    assert c.fix is None


def test_pypi_index_poisoned_lock_is_fail(monkeypatch, tmp_path: Path):
    _clear_index_env(monkeypatch)
    (tmp_path / "uv.lock").write_text(
        'source = { registry = "https://pypi-proxy.dev.databricks.com/simple" }\n'
    )
    c = doctor.check_pypi_index(tmp_path)
    assert c.status is doctor.Status.FAIL
    assert "uv.lock" in c.detail
    assert c.fix is not None


def test_pypi_index_env_var_is_warn(monkeypatch, tmp_path: Path):
    _clear_index_env(monkeypatch)
    monkeypatch.setenv("UV_INDEX_URL", "https://pypi-proxy.dev.databricks.com/simple")
    c = doctor.check_pypi_index(tmp_path)  # no lock present
    assert c.status is doctor.Status.WARN
    assert "UV_INDEX_URL" in c.detail
    assert "UV_INDEX_URL" in c.fix


def test_pypi_index_poisoned_lock_outranks_env(monkeypatch, tmp_path: Path):
    _clear_index_env(monkeypatch)
    monkeypatch.setenv("UV_INDEX_URL", "https://pypi-proxy.dev.databricks.com/simple")
    (tmp_path / "uv.lock").write_text("pypi-proxy.dev.databricks.com")
    c = doctor.check_pypi_index(tmp_path)
    assert c.status is doctor.Status.FAIL  # the on-disk lock is the blocking case


def test_pypi_index_unknown_mirror_is_warn(monkeypatch, tmp_path: Path):
    """#416: a non-public host other than the known Databricks proxy
    (Artifactory, a corp mirror) has no rewrite rule in the deploy sanitizer —
    doctor warns and names it, but doesn't hard-fail (it may be reachable)."""
    _clear_index_env(monkeypatch)
    (tmp_path / "uv.lock").write_text(
        'source = { registry = "https://artifactory.corp.example.com/api/pypi/simple" }\n'
    )
    c = doctor.check_pypi_index(tmp_path)
    assert c.status is doctor.Status.WARN
    assert "artifactory.corp.example.com" in c.detail
    assert c.fix is not None


def test_pypi_index_public_lock_with_git_source_is_ok(monkeypatch, tmp_path: Path):
    """#416: git+https sources (e.g. apx-agent pinned from GitHub) are not
    package indexes and must not trip the unknown-mirror warning."""
    _clear_index_env(monkeypatch)
    (tmp_path / "uv.lock").write_text(
        'source = { registry = "https://pypi.org/simple" }\n'
        'source = { git = "https://github.com/stuagano/apx-agent.git?rev=abc" }\n'
        'url = "https://files.pythonhosted.org/x/foo-1.0-py3-none-any.whl"\n'
    )
    c = doctor.check_pypi_index(tmp_path)
    assert c.status is doctor.Status.OK


def test_databricks_cli_missing_is_warn(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    c = doctor.check_databricks_cli()
    assert c.status is doctor.Status.WARN
    assert "deploy" in c.detail


def test_uvicorn_present(monkeypatch):
    monkeypatch.setattr(doctor.importlib, "import_module", lambda m: object())
    c = doctor.check_uvicorn()
    assert c.status is doctor.Status.OK


def test_uvicorn_missing_is_warn(monkeypatch):
    def boom(m):
        raise ImportError("no module")

    monkeypatch.setattr(doctor.importlib, "import_module", boom)
    c = doctor.check_uvicorn()
    assert c.status is doctor.Status.WARN
    assert c.fix is not None


def test_auth_ok(monkeypatch):
    with patch("databricks.sdk.core.Config", return_value=object()):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.OK


def test_auth_no_profiles_first_timer(monkeypatch):
    def boom(*a, **k):
        raise ValueError("no creds")

    with patch("databricks.sdk.core.Config", side_effect=boom), patch(
        "apx_agent.cli._databrickscfg_profiles", return_value=[]
    ):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.FAIL
    assert "auth login" in c.fix


def test_auth_ambiguous_profiles(monkeypatch):
    def boom(*a, **k):
        raise ValueError("ambiguous")

    with patch("databricks.sdk.core.Config", side_effect=boom), patch(
        "apx_agent.cli._databrickscfg_profiles", return_value=["DEFAULT", "prod"]
    ):
        c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.FAIL
    assert "DATABRICKS_CONFIG_PROFILE" in c.fix
    assert "prod" in c.fix


def test_workspace_skipped_when_auth_failed():
    c = doctor.check_databricks_workspace(auth_ok=False)
    assert c.status is doctor.Status.SKIP


def test_workspace_ok():
    me = MagicMock()
    me.user_name = "alice@example.com"
    client = MagicMock()
    client.current_user.me.return_value = me
    client.config.host = "https://x.cloud.databricks.com"
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.OK
    assert "alice@example.com" in c.detail


def test_workspace_expired_token():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("401 invalid access token")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "auth login" in c.fix


def test_workspace_unreachable():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("Name or service not known")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "host" in c.fix.lower()


def test_workspace_forbidden():
    client = MagicMock()
    client.current_user.me.side_effect = Exception("403 PERMISSION_DENIED")
    with patch("databricks.sdk.WorkspaceClient", return_value=client):
        c = doctor.check_databricks_workspace(auth_ok=True)
    assert c.status is doctor.Status.FAIL
    assert "permission" in c.detail.lower() or "403" in c.detail


def _make_apps_project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.apx.agent]\nname='x'\n"
    )
    (tmp_path / "agent_server").mkdir()
    (tmp_path / "agent_server" / "start_server.py").write_text("# app\n")
    (tmp_path / "databricks.yml").write_text("bundle:\n  name: x\n")
    return tmp_path


def test_project_layout_missing(tmp_path: Path):
    c = doctor.check_project_layout(tmp_path)
    assert c.status is doctor.Status.SKIP
    assert c.fix is not None and "scaffold" in c.fix


def test_project_layout_apps(tmp_path: Path):
    _make_apps_project(tmp_path)
    c = doctor.check_project_layout(tmp_path)
    assert c.status is doctor.Status.OK


def test_target_apps(tmp_path: Path):
    _make_apps_project(tmp_path)
    c = doctor.check_target(tmp_path)
    assert c.status is doctor.Status.OK
    assert "apps" in c.detail


def test_databricks_yml_present(tmp_path: Path):
    _make_apps_project(tmp_path)
    c = doctor.check_databricks_yml(tmp_path)
    assert c.status is doctor.Status.OK


def test_databricks_yml_missing_in_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[tool.apx.agent]\nname='x'\n")
    (tmp_path / "agent.py").write_text("# agent\n")
    c = doctor.check_databricks_yml(tmp_path)
    assert c.status is doctor.Status.WARN
    assert c.fix is not None


def test_databricks_yml_skip_outside_project(tmp_path: Path):
    c = doctor.check_databricks_yml(tmp_path)
    assert c.status is doctor.Status.SKIP


def _make_langgraph_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.apx.agent]\nname='x'\n")
    # app.py without databricks.yml → model-serving layout
    (tmp_path / "app.py").write_text("# model-serving entry point\n")
    return tmp_path


def test_extras_apps_installed(tmp_path, monkeypatch):
    _make_apps_project(tmp_path)
    monkeypatch.setattr(doctor.importlib, "import_module", lambda m: None)
    c = doctor.check_extras(tmp_path)
    assert c.status is doctor.Status.OK
    assert "mlflow" in c.detail.lower() or "eval" in c.detail.lower()


def test_extras_apps_missing(tmp_path, monkeypatch):
    _make_apps_project(tmp_path)

    def boom(m):
        raise ImportError("no module")

    monkeypatch.setattr(doctor.importlib, "import_module", boom)
    c = doctor.check_extras(tmp_path)
    assert c.status is doctor.Status.FAIL
    assert "eval" in c.fix or "mlflow" in c.fix.lower()


def test_extras_langgraph_missing(tmp_path, monkeypatch):
    _make_langgraph_project(tmp_path)

    def boom(m):
        raise ImportError("no module")

    monkeypatch.setattr(doctor.importlib, "import_module", boom)
    c = doctor.check_extras(tmp_path)
    assert c.status is doctor.Status.FAIL
    assert "langgraph" in c.fix


def test_apx_install_found(monkeypatch):
    monkeypatch.setattr(doctor.importlib.metadata, "version", lambda _: "1.2.3")
    c = doctor.check_apx_install()
    assert c.status is doctor.Status.OK
    assert "1.2.3" in c.detail


def test_apx_install_not_found(monkeypatch):
    def boom(_):
        raise doctor.importlib.metadata.PackageNotFoundError("apx-agent")

    monkeypatch.setattr(doctor.importlib.metadata, "version", boom)
    c = doctor.check_apx_install()
    assert c.status is doctor.Status.OK
    assert "editable" in c.detail


class TestCheckDeclaredTools:
    def _project(self, tmp_path: Path, tools_toml: str = "") -> Path:
        (tmp_path / "pyproject.toml").write_text(
            f"[tool.apx.agent]\nname='x'\n{tools_toml}"
        )
        (tmp_path / "agent.py").write_text("# agent\n")
        return tmp_path

    def test_no_tools_section_returns_empty(self, tmp_path):
        self._project(tmp_path)
        result = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert result == []

    def test_not_apx_project_returns_empty(self, tmp_path):
        result = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert result == []

    def test_auth_not_ok_skips_with_check_per_resource(self, tmp_path):
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'genie'\nspace_id = 'abc123'\n"
            "\n[[tool.apx.tools]]\ntype = 'vector_search'\nindex_name = 'cat.sch.idx'\n",
        )
        checks = doctor.check_declared_tools(tmp_path, auth_ok=False)
        assert len(checks) == 2
        assert all(c.status is Status.SKIP for c in checks)

    def test_genie_found(self, tmp_path):
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'genie'\nspace_id = 'space-1'\n",
        )
        ws = MagicMock()
        with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
            checks = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert len(checks) == 1
        assert checks[0].status is Status.OK
        ws.genie.get_space.assert_called_once_with(space_id="space-1")

    def test_genie_not_found(self, tmp_path):
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'genie'\nspace_id = 'bad-space'\n",
        )
        ws = MagicMock()
        ws.genie.get_space.side_effect = Exception("404 not found")
        with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
            checks = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert checks[0].status is Status.WARN
        assert "bad-space" in checks[0].fix

    def test_vector_search_found(self, tmp_path):
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'vector_search'\nindex_name = 'cat.sch.idx'\n",
        )
        ws = MagicMock()
        with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
            checks = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert checks[0].status is Status.OK
        ws.vector_search_indexes.get_index.assert_called_once_with(index_name="cat.sch.idx")

    def test_uc_function_found(self, tmp_path):
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'uc_function'\nfunction_name = 'main.tools.my_fn'\n",
        )
        ws = MagicMock()
        with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
            checks = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert checks[0].status is Status.OK
        ws.functions.get.assert_called_once_with(name="main.tools.my_fn")

    def test_uc_function_toolkit_found(self, tmp_path):
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'uc_function_toolkit'\ncatalog_schema = 'main.tools'\n",
        )
        ws = MagicMock()
        with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
            checks = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert checks[0].status is Status.OK
        ws.schemas.get.assert_called_once_with(full_name="main.tools")

    def test_sql_warehouse_found(self, tmp_path):
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'sql'\nwarehouse_id = 'wh-abc'\n",
        )
        ws = MagicMock()
        with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
            checks = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert checks[0].status is Status.OK
        ws.warehouses.get.assert_called_once_with(id="wh-abc")

    def test_sql_no_warehouse_id_skipped(self, tmp_path):
        """sql_tool without warehouse_id is valid config — nothing to validate."""
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'sql'\n",
        )
        ws = MagicMock()
        with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
            checks = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert checks == []

    def test_non_resource_type_ignored(self, tmp_path):
        """openapi/http/mcp tools don't need presence-checks here."""
        self._project(
            tmp_path,
            "\n[[tool.apx.tools]]\ntype = 'http'\nurl = 'http://localhost/api'\n",
        )
        ws = MagicMock()
        with patch("apx_agent._defaults._make_workspace_client", return_value=ws):
            checks = doctor.check_declared_tools(tmp_path, auth_ok=True)
        assert checks == []


def test_auth_sdk_not_importable(monkeypatch):
    """A broken/minimal install where databricks-sdk can't be imported FAILs
    fast with a clear message, rather than passing through to a deep traceback
    later (intentional hardening — databricks-sdk is a hard dependency)."""
    import sys
    import types

    # A stand-in module without `Config` makes `from databricks.sdk.core import
    # Config` raise ImportError, exercising the SDK-not-importable branch.
    fake = types.ModuleType("databricks.sdk.core")
    monkeypatch.setitem(sys.modules, "databricks.sdk.core", fake)
    c = doctor.check_databricks_auth()
    assert c.status is doctor.Status.FAIL
    assert "databricks-sdk not importable" in c.detail


class TestCheckModelEndpoint:
    def _make_pyproject(self, tmp_path: Path, model: str) -> None:
        (tmp_path / "pyproject.toml").write_text(
            f'[tool.apx.agent]\nmodel = "{model}"\n'
        )

    def test_no_pyproject_returns_none(self, tmp_path: Path):
        assert doctor.check_model_endpoint(tmp_path, auth_ok=True) is None

    def test_auth_not_ok_returns_none(self, tmp_path: Path):
        self._make_pyproject(tmp_path, "databricks-claude-sonnet-4-6")
        assert doctor.check_model_endpoint(tmp_path, auth_ok=False) is None

    def test_no_model_key_returns_none(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[tool.apx.agent]\n")
        assert doctor.check_model_endpoint(tmp_path, auth_ok=True) is None

    def test_endpoint_found_returns_ok(self, tmp_path: Path):
        self._make_pyproject(tmp_path, "databricks-claude-sonnet-4-6")
        ws = MagicMock()
        ep = MagicMock()
        ep.state = None
        ws.serving_endpoints.get.return_value = ep
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_model_endpoint(tmp_path, auth_ok=True)
        assert c is not None
        assert c.status is Status.OK
        ws.serving_endpoints.get.assert_called_once_with("databricks-claude-sonnet-4-6")

    def test_endpoint_not_found_returns_fail(self, tmp_path: Path):
        self._make_pyproject(tmp_path, "databricks-claude-sonnet-4-6")
        ws = MagicMock()
        ws.serving_endpoints.get.side_effect = Exception("404 Not Found")
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_model_endpoint(tmp_path, auth_ok=True)
        assert c is not None
        assert c.status is Status.FAIL
        assert "not found" in c.detail
        assert "pyproject.toml" in c.fix

    def test_endpoint_other_error_returns_warn(self, tmp_path: Path):
        self._make_pyproject(tmp_path, "databricks-claude-sonnet-4-6")
        ws = MagicMock()
        ws.serving_endpoints.get.side_effect = Exception("connection timeout")
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_model_endpoint(tmp_path, auth_ok=True)
        assert c is not None
        assert c.status is Status.WARN


class TestCheckUcDataSource:
    def _project(self, tmp_path: Path, catalog: str, schema: str) -> None:
        (tmp_path / "pyproject.toml").write_text(
            f'[tool.apx.agent]\nname = "x"\n'
            f'[tool.apx.agent.template]\nname = "data"\n'
            f'catalog = "{catalog}"\nschema = "{schema}"\n'
        )

    def test_no_pyproject_returns_none(self, tmp_path: Path):
        assert doctor.check_uc_data_source(tmp_path, auth_ok=True) is None

    def test_auth_not_ok_returns_none(self, tmp_path: Path):
        self._project(tmp_path, "main", "sales")
        assert doctor.check_uc_data_source(tmp_path, auth_ok=False) is None

    def test_no_template_catalog_returns_none(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text('[tool.apx.agent]\nname = "x"\n')
        assert doctor.check_uc_data_source(tmp_path, auth_ok=True) is None

    def test_schema_found_returns_ok(self, tmp_path: Path):
        self._project(tmp_path, "main", "sales")
        ws = MagicMock()
        ws.schemas.get.return_value = MagicMock()
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_uc_data_source(tmp_path, auth_ok=True)
        assert c is not None
        assert c.status is Status.OK
        ws.schemas.get.assert_called_once_with(full_name="main.sales")

    def test_schema_not_found_returns_fail(self, tmp_path: Path):
        self._project(tmp_path, "main", "sales")
        ws = MagicMock()
        ws.schemas.get.side_effect = Exception("404 Not Found")
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_uc_data_source(tmp_path, auth_ok=True)
        assert c is not None
        assert c.status is Status.FAIL
        assert "not found" in c.detail
        assert "USE SCHEMA" in c.fix

    def test_schema_permission_denied_returns_fail(self, tmp_path: Path):
        self._project(tmp_path, "main", "sales")
        ws = MagicMock()
        ws.schemas.get.side_effect = Exception("403 permission denied")
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_uc_data_source(tmp_path, auth_ok=True)
        assert c is not None
        assert c.status is Status.FAIL
        assert "USE SCHEMA" in c.fix


class TestCheckAppsEnabled:
    def test_auth_not_ok_returns_none(self):
        assert doctor.check_apps_enabled(auth_ok=False) is None

    def test_apps_enabled_returns_ok(self):
        ws = MagicMock()
        ws.apps.list.return_value = iter([])
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_apps_enabled(auth_ok=True)
        assert c is not None
        assert c.status is Status.OK

    def test_apps_disabled_returns_warn(self):
        ws = MagicMock()
        ws.apps.list.side_effect = Exception("404 feature not enabled")
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_apps_enabled(auth_ok=True)
        assert c is not None
        assert c.status is Status.WARN
        assert "model-serving" in c.fix

    def test_other_error_returns_none(self):
        ws = MagicMock()
        ws.apps.list.side_effect = Exception("network timeout")
        with patch("databricks.sdk.WorkspaceClient", return_value=ws):
            c = doctor.check_apps_enabled(auth_ok=True)
        assert c is None
