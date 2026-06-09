"""Tests for the YAML spec loader (_yaml_spec.py)."""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from apx_agent._yaml_spec import SpecValidationError, load_spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "spec.yaml"
    p.write_text(textwrap.dedent(content))
    return p


# ---------------------------------------------------------------------------
# Test 1: load_spec returns AgentConfig with correct name/model
# ---------------------------------------------------------------------------


def test_load_spec_basic(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """\
        name: my-agent
        model: databricks-claude-sonnet-4-6
        description: A test agent
        instructions: You are a test assistant.
        """,
    )
    config = load_spec(p)
    assert config.name == "my-agent"
    assert config.model == "databricks-claude-sonnet-4-6"
    assert config.description == "A test agent"
    assert config.instructions == "You are a test assistant."


# ---------------------------------------------------------------------------
# Test 2: coworker template fields (name, join_key) are preserved in config.template
# ---------------------------------------------------------------------------


def test_load_spec_template_fields_preserved(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """\
        name: payroll-coworker
        model: databricks-meta-llama-3-3-70b-instruct
        template:
          name: coworker
          catalog: main
          schema: sales
          join_key: employee_id
          persona: a payroll analyst
          memory: persistent
        """,
    )
    config = load_spec(p)
    assert config.template is not None
    assert config.template["name"] == "coworker"
    assert config.template["join_key"] == "employee_id"
    assert config.template["catalog"] == "main"
    assert config.template["schema"] == "sales"
    assert config.template["persona"] == "a payroll analyst"
    assert config.template["memory"] == "persistent"


# ---------------------------------------------------------------------------
# Test 3: memory and session backend configs are populated
# ---------------------------------------------------------------------------


def test_load_spec_memory_and_session(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """\
        name: payroll-coworker
        memory:
          type: delta
          table_name: main.default.apx_payroll_memory
          auto_create: true
          validate_at_boot: true
        session:
          type: delta
          table_name: main.default.apx_payroll_sessions
          auto_create: true
          validate_at_boot: true
        """,
    )
    config = load_spec(p)
    assert config.memory is not None
    assert config.memory.type == "delta"
    assert config.memory.table_name == "main.default.apx_payroll_memory"
    assert config.memory.auto_create is True
    assert config.memory.validate_at_boot is True

    assert config.session is not None
    assert config.session.type == "delta"
    assert config.session.table_name == "main.default.apx_payroll_sessions"
    assert config.session.auto_create is True
    assert config.session.validate_at_boot is True


# ---------------------------------------------------------------------------
# Test 4: env var substitution works ($CATALOG → actual value from os.environ)
# ---------------------------------------------------------------------------


def test_load_spec_env_var_substitution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATALOG", "prod_catalog")
    monkeypatch.setenv("SCHEMA", "hr_schema")
    monkeypatch.setenv("WAREHOUSE_ID", "abc123")

    p = _write_yaml(
        tmp_path,
        """\
        name: payroll-coworker
        template:
          name: coworker
          catalog: $CATALOG
          schema: $SCHEMA
          warehouse_id: ${WAREHOUSE_ID}
        memory:
          type: delta
          table_name: $CATALOG.$SCHEMA.apx_payroll_memory
        """,
    )
    config = load_spec(p)
    assert config.template is not None
    assert config.template["catalog"] == "prod_catalog"
    assert config.template["schema"] == "hr_schema"
    assert config.template["warehouse_id"] == "abc123"
    assert config.memory is not None
    assert config.memory.table_name == "prod_catalog.hr_schema.apx_payroll_memory"


# ---------------------------------------------------------------------------
# Test 5: missing name field raises SpecValidationError
# ---------------------------------------------------------------------------


def test_load_spec_missing_name_raises(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """\
        model: databricks-meta-llama-3-3-70b-instruct
        description: No name here
        """,
    )
    with pytest.raises(SpecValidationError, match="missing required field.*name"):
        load_spec(p)


# ---------------------------------------------------------------------------
# Test 6: nonexistent file raises FileNotFoundError
# ---------------------------------------------------------------------------


def test_load_spec_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.yaml"
    with pytest.raises(FileNotFoundError, match="Spec file not found"):
        load_spec(missing)


# ---------------------------------------------------------------------------
# Unresolved placeholder validation
# ---------------------------------------------------------------------------


def test_load_spec_unresolved_catalog_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$CATALOG left unresolved → SpecValidationError naming the field."""
    monkeypatch.delenv("CATALOG", raising=False)
    monkeypatch.delenv("SCHEMA", raising=False)

    p = _write_yaml(
        tmp_path,
        """\
        name: payroll-coworker
        template:
          name: coworker
          catalog: $CATALOG
          schema: $SCHEMA
        memory:
          type: delta
          table_name: $CATALOG.$SCHEMA.apx_payroll_memory
        """,
    )
    with pytest.raises(SpecValidationError) as exc:
        load_spec(p)
    msg = str(exc.value)
    assert "CATALOG" in msg
    assert "SCHEMA" in msg
    # Fix hint should suggest re-scaffolding with the relevant flags.
    assert "--catalog" in msg
    assert "--schema" in msg


def test_load_spec_unresolved_custom_var_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An arbitrary unresolved $VAR surfaces the variable name and export hint."""
    monkeypatch.delenv("MY_SECRET", raising=False)

    p = _write_yaml(
        tmp_path,
        """\
        name: demo
        model: databricks-claude-sonnet-4-6
        instructions: key is $MY_SECRET
        """,
    )
    with pytest.raises(SpecValidationError) as exc:
        load_spec(p)
    msg = str(exc.value)
    assert "MY_SECRET" in msg
    assert "export MY_SECRET" in msg


def test_load_spec_resolved_vars_do_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When all $VARs are resolved, load_spec succeeds."""
    monkeypatch.setenv("CATALOG", "main")
    monkeypatch.setenv("SCHEMA", "hr")

    p = _write_yaml(
        tmp_path,
        """\
        name: payroll-coworker
        template:
          name: coworker
          catalog: $CATALOG
          schema: $SCHEMA
        memory:
          type: delta
          table_name: $CATALOG.$SCHEMA.apx_payroll_memory
        """,
    )
    config = load_spec(p)
    assert config.template is not None
    assert config.template["catalog"] == "main"


def test_collect_unresolved_finds_nested_vars(tmp_path: Path) -> None:
    """_collect_unresolved locates $VARs at arbitrary nesting depth."""
    from apx_agent._yaml_spec import _collect_unresolved

    data = {
        "name": "demo",
        "template": {"catalog": "$CATALOG", "schema": "fixed"},
        "memory": {"table_name": "$CATALOG.$SCHEMA.tbl"},
    }
    hits = _collect_unresolved(data)
    var_names = {var for _, var in hits}
    assert "CATALOG" in var_names
    assert "SCHEMA" in var_names
    assert "fixed" not in var_names
