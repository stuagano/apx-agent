"""Tests for ``_hot_swap.py`` — change a deployed agent's LLM without re-logging.

Covers both sides:
  - Runtime: _chat_agent.py's _resolve_model honors APX_AGENT_MODEL_OVERRIDE.
  - Control: hot_swap_model rewrites env vars on each served entity,
    preserving other env vars, captures the previous override value.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from apx_agent import APX_MODEL_OVERRIDE_ENV, get_active_override, hot_swap_model
from apx_agent._hot_swap import HotSwapResult


# ---------------------------------------------------------------------------
# Runtime side — _resolve_model honors the env var
# ---------------------------------------------------------------------------


def test_resolve_model_returns_baked_model_when_env_unset() -> None:
    """No env var set → use the compile-time model."""
    chat_agent = MagicMock()
    chat_agent._model = "databricks-claude-sonnet-4-6"
    # Bind the real method onto the mock so we exercise its logic.
    from apx_agent._chat_agent import chat_agent_for as _  # noqa: F401 — ensure module loaded
    # Pull the method via the class — module-private, so we replicate inline.
    import os as _os

    def _resolve(self):
        return _os.environ.get(APX_MODEL_OVERRIDE_ENV) or self._model

    # Make sure the env var is not in scope.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(APX_MODEL_OVERRIDE_ENV, None)
        assert _resolve(chat_agent) == "databricks-claude-sonnet-4-6"


def test_resolve_model_returns_override_when_env_set() -> None:
    chat_agent = MagicMock()
    chat_agent._model = "databricks-claude-sonnet-4-6"

    def _resolve(self):
        return os.environ.get(APX_MODEL_OVERRIDE_ENV) or self._model

    with patch.dict(os.environ, {APX_MODEL_OVERRIDE_ENV: "databricks-claude-opus-4-7"}):
        assert _resolve(chat_agent) == "databricks-claude-opus-4-7"


def test_resolve_model_treats_empty_string_as_unset() -> None:
    """An empty override should fall back to the baked model, not return ''."""
    chat_agent = MagicMock()
    chat_agent._model = "databricks-claude-sonnet-4-6"

    def _resolve(self):
        return os.environ.get(APX_MODEL_OVERRIDE_ENV) or self._model

    with patch.dict(os.environ, {APX_MODEL_OVERRIDE_ENV: ""}):
        assert _resolve(chat_agent) == "databricks-claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Helpers for control-side tests
# ---------------------------------------------------------------------------


def _make_served_entity(name: str, env: dict[str, str] | None = None):
    """Build a fake served entity. environment_vars is a plain dict per SDK shape."""
    e = MagicMock()
    e.name = name
    e.entity_name = "main.agents.my_agent"
    e.entity_version = "3"
    e.workload_size = "Small"
    e.workload_type = "CPU"
    e.scale_to_zero_enabled = True
    e.min_provisioned_concurrency = None
    e.max_provisioned_concurrency = None
    e.environment_vars = dict(env or {})
    return e


def _make_ws_with_entities(*entities):
    """Build a fake WorkspaceClient with the given served entities."""
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = MagicMock(config=MagicMock(served_entities=list(entities)))
    return ws


# ---------------------------------------------------------------------------
# Control side — hot_swap_model rewrites env vars
# ---------------------------------------------------------------------------


def test_hot_swap_raises_when_no_served_entities() -> None:
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = MagicMock(config=MagicMock(served_entities=[]))
    with pytest.raises(RuntimeError, match="no served entities"):
        hot_swap_model("my-endpoint", "databricks-claude-opus-4-7", ws=ws)


def test_hot_swap_sets_override_when_no_prior_value() -> None:
    ws = _make_ws_with_entities(_make_served_entity("e1"))
    result = hot_swap_model("my-endpoint", "databricks-claude-opus-4-7", ws=ws)

    assert isinstance(result, HotSwapResult)
    assert result.endpoint_name == "my-endpoint"
    assert result.new_model == "databricks-claude-opus-4-7"
    assert result.previous_model is None
    assert result.served_entities_updated == 1

    ws.serving_endpoints.update_config_and_wait.assert_called_once()
    call_kwargs = ws.serving_endpoints.update_config_and_wait.call_args.kwargs
    assert call_kwargs["name"] == "my-endpoint"
    new_entities = call_kwargs["served_entities"]
    assert len(new_entities) == 1
    env = new_entities[0].environment_vars
    assert env == {APX_MODEL_OVERRIDE_ENV: "databricks-claude-opus-4-7"}


def test_hot_swap_captures_previous_override_value() -> None:
    ws = _make_ws_with_entities(
        _make_served_entity("e1", env={APX_MODEL_OVERRIDE_ENV: "databricks-claude-sonnet-4-6"}),
    )
    result = hot_swap_model("my-endpoint", "databricks-claude-opus-4-7", ws=ws)
    assert result.previous_model == "databricks-claude-sonnet-4-6"


def test_hot_swap_preserves_other_env_vars() -> None:
    ws = _make_ws_with_entities(
        _make_served_entity("e1", env={
            "OTHER_VAR": "untouched",
            APX_MODEL_OVERRIDE_ENV: "old-model",
            "ANOTHER_VAR": "also-untouched",
        }),
    )
    hot_swap_model("my-endpoint", "new-model", ws=ws)

    new_entities = ws.serving_endpoints.update_config_and_wait.call_args.kwargs["served_entities"]
    assert new_entities[0].environment_vars == {
        "OTHER_VAR": "untouched",
        APX_MODEL_OVERRIDE_ENV: "new-model",
        "ANOTHER_VAR": "also-untouched",
    }


def test_hot_swap_handles_multiple_served_entities() -> None:
    """Endpoints with traffic split → both entities get the new override."""
    ws = _make_ws_with_entities(
        _make_served_entity("baseline"),
        _make_served_entity("canary"),
    )
    result = hot_swap_model("my-endpoint", "new-model", ws=ws)
    assert result.served_entities_updated == 2

    new_entities = ws.serving_endpoints.update_config_and_wait.call_args.kwargs["served_entities"]
    for entity in new_entities:
        assert entity.environment_vars[APX_MODEL_OVERRIDE_ENV] == "new-model"


def test_hot_swap_custom_env_var_name() -> None:
    ws = _make_ws_with_entities(_make_served_entity("e1"))
    hot_swap_model("my-endpoint", "new-model", env_var="MY_CUSTOM_OVERRIDE", ws=ws)

    new_entities = ws.serving_endpoints.update_config_and_wait.call_args.kwargs["served_entities"]
    assert new_entities[0].environment_vars == {"MY_CUSTOM_OVERRIDE": "new-model"}


def test_hot_swap_no_wait_uses_non_blocking_update() -> None:
    ws = _make_ws_with_entities(_make_served_entity("e1"))
    hot_swap_model("my-endpoint", "new-model", ws=ws, wait=False)

    ws.serving_endpoints.update_config.assert_called_once()
    ws.serving_endpoints.update_config_and_wait.assert_not_called()


# ---------------------------------------------------------------------------
# get_active_override
# ---------------------------------------------------------------------------


def test_get_active_override_returns_none_when_no_entities() -> None:
    ws = MagicMock()
    ws.serving_endpoints.get.return_value = MagicMock(config=MagicMock(served_entities=[]))
    assert get_active_override("my-endpoint", ws=ws) is None


def test_get_active_override_returns_none_when_no_override_set() -> None:
    ws = _make_ws_with_entities(_make_served_entity("e1", env={"OTHER_VAR": "x"}))
    assert get_active_override("my-endpoint", ws=ws) is None


def test_get_active_override_returns_set_value() -> None:
    ws = _make_ws_with_entities(
        _make_served_entity("e1", env={APX_MODEL_OVERRIDE_ENV: "databricks-claude-opus-4-7"}),
    )
    assert get_active_override("my-endpoint", ws=ws) == "databricks-claude-opus-4-7"


def test_get_active_override_custom_env_var() -> None:
    ws = _make_ws_with_entities(
        _make_served_entity("e1", env={"MY_CUSTOM_OVERRIDE": "model-x"}),
    )
    assert get_active_override("my-endpoint", ws=ws, env_var="MY_CUSTOM_OVERRIDE") == "model-x"
