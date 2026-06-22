"""Tests for _inspection.py — function introspection, schema generation, config loading."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import BaseModel

from apx_agent._inspection import (
    _inspect_tool_fn,
    _is_fastapi_dependency,
    _load_agent_config,
    _make_input_model,
    _make_route_handler,
    _schema_for_model,
    _schema_for_return,
)

from .conftest import (
    FakeWorkspaceDep,
    StructuredOutput,
    async_tool,
    get_weather,
    no_args,
    query_genie,
    structured_tool,
)


# ---------------------------------------------------------------------------
# _is_fastapi_dependency
# ---------------------------------------------------------------------------


class TestIsFastapiDependency:
    def test_recognizes_depends_annotation(self):
        assert _is_fastapi_dependency(FakeWorkspaceDep) is True

    def test_rejects_plain_type(self):
        assert _is_fastapi_dependency(str) is False
        assert _is_fastapi_dependency(int) is False

    def test_rejects_none(self):
        assert _is_fastapi_dependency(None) is False


# ---------------------------------------------------------------------------
# _inspect_tool_fn
# ---------------------------------------------------------------------------


class TestInspectToolFn:
    def test_plain_params_only(self):
        plain, deps = _inspect_tool_fn(get_weather)
        assert "city" in plain
        assert "country_code" in plain
        assert deps == []
        # Default value preserved
        assert plain["country_code"][1] == "US"

    def test_mixed_params_and_deps(self):
        plain, deps = _inspect_tool_fn(query_genie)
        assert "question" in plain
        assert "space_id" in plain
        assert "ws" not in plain
        assert "ws" in deps

    def test_deps_only(self):
        plain, deps = _inspect_tool_fn(no_args)
        assert plain == {}
        assert "ws" in deps

    def test_async_function(self):
        plain, deps = _inspect_tool_fn(async_tool)
        assert "query" in plain
        assert deps == []


# ---------------------------------------------------------------------------
# _make_input_model
# ---------------------------------------------------------------------------


class TestMakeInputModel:
    def test_creates_model_from_params(self):
        plain, _ = _inspect_tool_fn(get_weather)
        model = _make_input_model(get_weather, plain)
        assert model is not None
        assert "city" in model.model_fields
        assert "country_code" in model.model_fields

    def test_excludes_dependencies(self):
        plain, _ = _inspect_tool_fn(query_genie)
        model = _make_input_model(query_genie, plain)
        assert model is not None
        assert "ws" not in model.model_fields
        assert "question" in model.model_fields

    def test_returns_none_for_no_params(self):
        plain, _ = _inspect_tool_fn(no_args)
        model = _make_input_model(no_args, plain)
        assert model is None

    def test_model_validates_input(self):
        plain, _ = _inspect_tool_fn(get_weather)
        model = _make_input_model(get_weather, plain)
        instance = model(city="Portland", country_code="US")
        assert instance.city == "Portland"


# ---------------------------------------------------------------------------
# _make_route_handler
# ---------------------------------------------------------------------------


class TestMakeRouteHandler:
    def test_handler_with_body_has_correct_signature(self):
        plain, deps = _inspect_tool_fn(get_weather)
        model = _make_input_model(get_weather, plain)
        handler = _make_route_handler(get_weather, model, deps)
        sig = inspect.signature(handler)
        assert "body" in sig.parameters
        assert "ws" not in sig.parameters

    def test_handler_with_deps_has_correct_signature(self):
        plain, deps = _inspect_tool_fn(query_genie)
        model = _make_input_model(query_genie, plain)
        handler = _make_route_handler(query_genie, model, deps)
        sig = inspect.signature(handler)
        assert "body" in sig.parameters
        assert "ws" in sig.parameters

    def test_handler_no_body_when_no_plain_params(self):
        plain, deps = _inspect_tool_fn(no_args)
        model = _make_input_model(no_args, plain)
        handler = _make_route_handler(no_args, model, deps)
        sig = inspect.signature(handler)
        assert "body" not in sig.parameters
        assert "ws" in sig.parameters

    def test_handler_preserves_name_and_doc(self):
        plain, deps = _inspect_tool_fn(get_weather)
        model = _make_input_model(get_weather, plain)
        handler = _make_route_handler(get_weather, model, deps)
        assert handler.__name__ == "get_weather"
        assert handler.__doc__ is not None
        assert "weather" in handler.__doc__.lower()


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


class TestSchemaHelpers:
    def test_schema_for_model_with_fields(self):
        plain, _ = _inspect_tool_fn(get_weather)
        model = _make_input_model(get_weather, plain)
        schema = _schema_for_model(model)
        assert schema is not None
        assert "properties" in schema
        assert "city" in schema["properties"]

    def test_schema_for_model_none(self):
        assert _schema_for_model(None) is None

    def test_schema_for_return_string(self):
        schema = _schema_for_return(get_weather)
        assert schema == {"type": "string"}

    def test_schema_for_return_pydantic(self):
        schema = _schema_for_return(structured_tool)
        assert schema is not None
        assert "properties" in schema
        assert "answer" in schema["properties"]

    def test_schema_for_return_none(self):
        def no_return_hint():
            pass
        schema = _schema_for_return(no_return_hint)
        assert schema is None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestLoadAgentConfig:
    def test_loads_from_pyproject(self):
        config = _load_agent_config()
        # pyproject.toml in the repo root has [tool.apx.agent]
        assert config is not None
        assert config.name == "apx-agent"
        assert config.model == "databricks-meta-llama-3-3-70b-instruct"

    def test_returns_none_for_missing_section(self):
        config = _load_agent_config(section_path=("tool", "nonexistent", "section"))
        assert config is None

    def test_explicit_pyproject_path(self, tmp_path):
        """Explicit pyproject_path overrides all heuristics."""
        toml = tmp_path / "pyproject.toml"
        toml.write_text(
            '[tool.apx.agent]\nname = "explicit-test"\ndescription = "from path"\n'
        )
        config = _load_agent_config(pyproject_path=toml)
        assert config is not None
        assert config.name == "explicit-test"

    def test_explicit_pyproject_path_missing(self, tmp_path):
        """Explicit path to a non-existent file returns None."""
        config = _load_agent_config(pyproject_path=tmp_path / "nope.toml")
        assert config is None

    def test_env_pyproject_override(self, tmp_path, monkeypatch):
        """APX_PYPROJECT pins resolution ahead of the __main__/cwd heuristics.

        This is what `apx-agent run <spec>.yaml` relies on so the generated
        temp project's config wins over a nearer pyproject.
        """
        toml = tmp_path / "pyproject.toml"
        toml.write_text('[tool.apx.agent]\nname = "from-env"\n')
        monkeypatch.setenv("APX_PYPROJECT", str(toml))
        config = _load_agent_config()
        assert config is not None
        assert config.name == "from-env"

    def test_explicit_path_outranks_env(self, tmp_path, monkeypatch):
        """An explicit pyproject_path still beats APX_PYPROJECT."""
        env_toml = tmp_path / "env" / "pyproject.toml"
        env_toml.parent.mkdir()
        env_toml.write_text('[tool.apx.agent]\nname = "from-env"\n')
        arg_toml = tmp_path / "arg" / "pyproject.toml"
        arg_toml.parent.mkdir()
        arg_toml.write_text('[tool.apx.agent]\nname = "from-arg"\n')
        monkeypatch.setenv("APX_PYPROJECT", str(env_toml))
        config = _load_agent_config(pyproject_path=arg_toml)
        assert config is not None
        assert config.name == "from-arg"


def test_load_agent_config_parses_examples(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.apx.agent]\n'
        'name = "demo"\n'
        'examples = ["Show me sample customers", "Top 5 by balance"]\n'
    )
    cfg = _load_agent_config(pyproject_path=str(pyproject))
    assert cfg is not None
    assert cfg.examples == ["Show me sample customers", "Top 5 by balance"]


def test_load_agent_config_examples_defaults_empty(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[tool.apx.agent]\nname = "demo"\n')
    cfg = _load_agent_config(pyproject_path=str(pyproject))
    assert cfg is not None
    assert cfg.examples == []
