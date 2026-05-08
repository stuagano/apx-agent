from unittest.mock import MagicMock, patch, call
from tools.scaffold_project import _generate_files, scaffold_project, GenieSpace


def test_generate_files_returns_required_files():
    files = _generate_files("answer sales questions", ["main.sales.orders"], [], "mcp-sales")
    assert set(files.keys()) == {"app.py", "pyproject.toml", "requirements.txt", "app.yml"}


def test_generate_files_app_yml_specifies_port():
    files = _generate_files("answer sales questions", ["main.sales.orders"], [], "mcp-sales")
    assert "$DATABRICKS_APP_PORT" in files["app.yml"]


def test_generate_files_pyproject_subdirectory():
    files = _generate_files("answer sales questions", ["main.sales.orders"], [], "mcp-sales")
    assert "#subdirectory=python" in files["pyproject.toml"]


def test_generate_files_requirements_txt_subdirectory():
    files = _generate_files("answer sales questions", ["main.sales.orders"], [], "mcp-sales")
    assert "#subdirectory=python" in files["requirements.txt"]


def test_generate_files_app_py_includes_sql_tool_for_each_table():
    files = _generate_files("answer sales questions", ["main.sales.orders", "main.sales.customers"], [], "mcp-sales")
    assert "main.sales.orders" in files["app.py"]
    assert "main.sales.customers" in files["app.py"]
    assert "sql_tool" in files["app.py"]


def test_generate_files_app_py_includes_genie_tool_for_space():
    files = _generate_files("answer sales questions", [], [GenieSpace(id="abc123", name="Sales")], "mcp-sales")
    assert "abc123" in files["app.py"]
    assert "genie_tool" in files["app.py"]


def test_generate_files_includes_lineage_tool_when_requested():
    files = _generate_files("explore lineage", ["main.sales.orders"], [], "mcp-lineage", include_lineage=True)
    assert "lineage_tool" in files["app.py"]


def test_generate_files_no_lineage_by_default():
    files = _generate_files("answer questions", ["main.sales.orders"], [], "mcp-agent")
    assert "lineage_tool" not in files["app.py"]


def test_generate_files_app_name_in_pyproject():
    files = _generate_files("test", ["a.b.c"], [], "mcp-test-agent")
    assert "mcp-test-agent" in files["pyproject.toml"]


def test_generate_files_use_case_in_instructions():
    files = _generate_files("handle customer refunds", ["main.billing.transactions"], [], "mcp-refunds")
    assert "handle customer refunds" in files["app.py"]


def test_scaffold_project_uploads_all_files():
    ws = MagicMock()
    ws.current_user.me.return_value = MagicMock(user_name="user@example.com")

    with patch("tools.scaffold_project._generate_files") as mock_gen, \
         patch("tools.scaffold_project._upload_files") as mock_upload:
        mock_gen.return_value = {"app.py": "code", "pyproject.toml": "toml", "app.yml": "yaml"}

        result = scaffold_project("test", ["a.b.c"], [], "mcp-test", False, ws)

    mock_upload.assert_called_once()
    upload_args = mock_upload.call_args[0]
    assert upload_args[0] is ws
    assert upload_args[1] == {"app.py": "code", "pyproject.toml": "toml", "app.yml": "yaml"}
    assert "mcp-test" in upload_args[2]
    assert "user@example.com" in upload_args[2]
    assert "mcp-test" in result
