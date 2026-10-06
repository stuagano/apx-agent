"""Tests for ``apx_agent._llm`` — the provider-compat factory."""

from __future__ import annotations

import pytest

# These imports require databricks-langchain to be installed; if it's not,
# the factory itself can't be exercised.
pytest.importorskip("databricks_langchain")

from databricks_langchain import ChatDatabricks  # noqa: E402

from apx_agent import ChatDatabricksGptReasoning, get_llm  # noqa: E402
from apx_agent._llm import GPT_REASONING_PREFIXES  # noqa: E402


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def test_claude_returns_plain_chat_databricks():
    llm = get_llm("databricks-claude-sonnet-4-6")
    assert isinstance(llm, ChatDatabricks)
    # Plain ChatDatabricks, NOT the reasoning subclass.
    assert type(llm).__name__ == "ChatDatabricks"
    assert llm.use_ai_gateway is True


def test_llama_returns_plain_chat_databricks():
    llm = get_llm("databricks-meta-llama-3-3-70b-instruct")
    assert type(llm).__name__ == "ChatDatabricks"
    assert llm.use_ai_gateway is True


def test_gemini_returns_plain_chat_databricks():
    llm = get_llm("databricks-gemini-2-5-flash")
    assert type(llm).__name__ == "ChatDatabricks"
    assert llm.use_ai_gateway is True


def test_gpt_5_returns_reasoning_subclass():
    llm = get_llm("databricks-gpt-5-mini")
    # Subclass of ChatDatabricks, named ChatDatabricksGptReasoning.
    assert isinstance(llm, ChatDatabricks)
    assert type(llm).__name__ == "ChatDatabricksGptReasoning"
    assert llm.use_ai_gateway is True


def test_get_llm_rejects_serving_transport_opt_out():
    """The chat model has one transport. Callers cannot turn the flag off."""
    with pytest.raises(ValueError, match="use_ai_gateway"):
        get_llm("databricks-claude-sonnet-4-6", use_ai_gateway=False)


@pytest.mark.parametrize("model", ["bedrock:anthropic.claude", "quantum:model", " model "])
def test_chat_model_rejects_unsupported_bindings(model):
    from apx_agent import AgentConfig

    for build in (lambda: AgentConfig(name="test", model=model), lambda: get_llm(model)):
        with pytest.raises(ValueError, match="Set model to an existing AI Gateway model service"):
            build()


@pytest.mark.parametrize("model", ["databricks-claude-sonnet-4-6", "main.ai.customer_chat"])
def test_gateway_model_binding_is_preserved(model):
    from apx_agent import AgentConfig

    assert AgentConfig(name="test", model=model).model == model
    assert get_llm(model).model == model


def test_gpt_5_5_pro_routes_to_reasoning_subclass():
    """All databricks-gpt-5* endpoints route to reasoning, including 5-5-pro.

    GPT-5-5-Pro actually requires the Responses API and the reasoning subclass
    won't fully fix it — but routing it to the reasoning class at least keeps
    the temperature/top_p strip honest. The eventual full fix is a separate
    Responses-API client; see docstring in apx_agent._llm.
    """
    llm = get_llm("databricks-gpt-5-5-pro")
    assert type(llm).__name__ == "ChatDatabricksGptReasoning"
    assert llm.use_ai_gateway is True


# ---------------------------------------------------------------------------
# Payload-shape — the subclass strips temperature and top_p
# ---------------------------------------------------------------------------

def test_gpt_reasoning_subclass_strips_temperature_and_top_p():
    """The reasoning subclass must remove both fields before the request goes out."""
    llm = get_llm("databricks-gpt-5-mini", temperature=0.5)

    # _prepare_inputs is the codepath that builds the outgoing request body.
    # We verify it strips the two fields the GPT-5 endpoint will reject.
    data = llm._prepare_inputs(
        messages=[],
        extra_params={"top_p": 0.9},
    )
    assert "temperature" not in data
    assert "top_p" not in data


def test_plain_chat_databricks_passes_temperature_through():
    """Claude/Llama/Gemini accept temperature — verify it survives _prepare_inputs."""
    llm = get_llm("databricks-claude-sonnet-4-6", temperature=0.5)
    data = llm._prepare_inputs(messages=[])
    # Plain ChatDatabricks should include temperature when set non-None
    assert data.get("temperature") == 0.5


# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------

def test_gpt_reasoning_prefixes_constant_is_tuple_of_strings():
    """Sanity check on the routing config so future contributors can find it."""
    assert isinstance(GPT_REASONING_PREFIXES, tuple)
    assert all(isinstance(p, str) for p in GPT_REASONING_PREFIXES)
    assert "databricks-gpt-5" in GPT_REASONING_PREFIXES


def test_kwargs_forwarded_to_constructor():
    """Caller kwargs (max_tokens, etc.) must reach the underlying ChatDatabricks."""
    llm = get_llm("databricks-claude-sonnet-4-6", max_tokens=42)
    assert llm.max_tokens == 42


def test_chat_databricks_gpt_reasoning_is_importable_from_root():
    """The named class is part of the public surface for isinstance checks."""
    from apx_agent import ChatDatabricksGptReasoning as Imported
    assert Imported is ChatDatabricksGptReasoning
