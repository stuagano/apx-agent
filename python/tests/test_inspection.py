"""Tests for _inspection.py — function introspection, schema generation, config loading."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import BaseModel

from apx_agent._inspection import (
    _tool_dependency_callables,
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

    def test_dependency_callables_reuse_inspected_dependency_parameters(self):
        dependencies = _tool_dependency_callables(query_genie)

        assert list(dependencies) == ["ws"]
        assert callable(dependencies["ws"])


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

    def test_schema_for_model_none_is_empty_object_schema(self):
        """Zero-arg tools advertise an empty-object schema, never null (#439).

        FMAPI rejects tool schemas whose additionalProperties is anything but
        False or absent; the A2A card must carry the same shape, not null.
        """
        schema = _schema_for_model(None)
        assert schema == {"type": "object", "properties": {}, "additionalProperties": False}

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
    def test_loads_from_cwd_pyproject(self, tmp_path, monkeypatch):
        """Default resolution finds the project pyproject in cwd."""
        (tmp_path / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "cwd-project"\nmodel = "some-model"\n'
        )
        monkeypatch.chdir(tmp_path)
        config = _load_agent_config()
        assert config is not None
        assert config.name == "cwd-project"
        assert config.model == "some-model"

    def test_runtime_agent_name_overrides_committed_name(self, tmp_path, monkeypatch):
        toml = tmp_path / "pyproject.toml"
        toml.write_text('[tool.apx.agent]\nname = "shared-agent"\n')
        monkeypatch.setenv("APX_AGENT_NAME", "pricing-agent")

        config = _load_agent_config(pyproject_path=toml)

        assert config is not None
        assert config.name == "pricing-agent"

    def test_blank_runtime_agent_name_is_rejected(self, tmp_path, monkeypatch):
        toml = tmp_path / "pyproject.toml"
        toml.write_text('[tool.apx.agent]\nname = "shared-agent"\n')
        monkeypatch.setenv("APX_AGENT_NAME", "  ")

        with pytest.raises(ValueError, match="APX_AGENT_NAME must not be blank"):
            _load_agent_config(pyproject_path=toml)

    def test_returns_none_for_missing_section(self, tmp_path, monkeypatch):
        (tmp_path / "pyproject.toml").write_text('[tool.apx.agent]\nname = "x"\n')
        monkeypatch.chdir(tmp_path)
        config = _load_agent_config(section_path=("tool", "nonexistent", "section"))
        assert config is None

    def test_cwd_project_wins_over_main_heuristic(self, tmp_path, monkeypatch):
        """#437: serving from the project directory must load the project's
        [tool.apx.agent], not one reachable from __main__ (the apx-agent
        console script lives in the framework venv — its walk-up used to find
        the framework repo's pyproject and silently shadow the project's)."""
        import sys

        framework = tmp_path / "framework"
        framework.mkdir()
        (framework / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "framework"\ndescription = "Example agent"\n'
        )
        (framework / "bin.py").write_text("")
        project = tmp_path / "project"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "agent-b"\n'
        )
        monkeypatch.setattr(
            sys.modules["__main__"], "__file__", str(framework / "bin.py"), raising=False
        )
        monkeypatch.chdir(project)
        config = _load_agent_config()
        assert config is not None
        assert config.name == "agent-b"

    def test_cwd_without_agent_config_falls_through_to_main(self, tmp_path, monkeypatch):
        """A cwd pyproject with no [tool.apx.agent] (or deploy-only keys) does
        not claim resolution — the __main__ walk-up still serves the
        run-from-elsewhere cases it was built for."""
        import sys

        entry_dir = tmp_path / "entry"
        entry_dir.mkdir()
        (entry_dir / "pyproject.toml").write_text(
            '[tool.apx.agent]\nname = "from-main"\n'
        )
        (entry_dir / "app.py").write_text("")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "pyproject.toml").write_text(
            '[tool.apx.agent]\nregistered_model = "main.agents.x"\n'
        )
        monkeypatch.setattr(
            sys.modules["__main__"], "__file__", str(entry_dir / "app.py"), raising=False
        )
        monkeypatch.chdir(elsewhere)
        config = _load_agent_config()
        assert config is not None
        assert config.name == "from-main"

    def test_resolution_logs_resolved_path_and_name(self, tmp_path, monkeypatch, caplog):
        """#437 visibility: one INFO line states which pyproject was resolved
        and the agent name, so shadowing is diagnosable."""
        import logging

        toml = tmp_path / "pyproject.toml"
        toml.write_text('[tool.apx.agent]\nname = "agent-b"\n')
        monkeypatch.chdir(tmp_path)
        with caplog.at_level(logging.INFO, logger="apx_agent._inspection"):
            config = _load_agent_config()
        assert config is not None
        assert any(
            "tool.apx.agent" in r.message and str(toml) in r.message and "name=agent-b" in r.message
            for r in caplog.records
        )

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
        toml = tmp_path / "env" / "pyproject.toml"
        toml.parent.mkdir()
        toml.write_text('[tool.apx.agent]\nname = "from-env"\n')
        cwd_project = tmp_path / "cwd"
        cwd_project.mkdir()
        (cwd_project / "pyproject.toml").write_text('[tool.apx.agent]\nname = "from-cwd"\n')
        monkeypatch.chdir(cwd_project)
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


def test_load_agent_config_parses_workflows(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.apx.agent]\n'
        'name = "demo"\n'
        'workflows = [{ id = "pricing-review", title = "Pricing review", '
        'question = "Show me the pricing evidence", purpose = "Move from signal to decision.", '
        'route = ["intelligence", "calibrate"], outcome = "Reviewable pricing packet" }]\n'
    )
    cfg = _load_agent_config(pyproject_path=str(pyproject))
    assert cfg is not None
    assert cfg.workflows[0].id == "pricing-review"
    assert cfg.workflows[0].route == ["intelligence", "calibrate"]


def test_load_agent_config_deploy_only_section_returns_none(tmp_path):
    """#407/#434 CI regression: a [tool.apx.agent] holding only deploy-envelope
    keys (registered_model, experiment) declares no agent config — the loader
    must return None, not crash AgentConfig validation on an empty field set."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[tool.apx.agent]\n'
        'registered_model = "main.agents.from_cfg"\n'
        'experiment = "/Users/me/exp"\n'
    )
    assert _load_agent_config(pyproject_path=str(pyproject)) is None
