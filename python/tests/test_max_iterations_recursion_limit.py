"""#632: max_iterations=0 must cap recursion, not disable the apx limit.

``_compile_llm_agent`` used ``if max_iter:`` which treats 0 as falsy and skips
setting ``recursion_limit`` — so an explicit zero cap silently fell through to
LangGraph's default instead of bounding the loop.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from apx_agent import LlmAgent
from apx_agent._compile import CompileContext, _compile_llm_agent


@pytest.fixture(autouse=True)
def _stub_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from apx_agent import _compile

    monkeypatch.setattr(
        _compile,
        "_build_chat_databricks",
        lambda endpoint, *, temperature=None, max_tokens=None: MagicMock(
            name=f"fake_llm:{endpoint}"
        ),
    )


def _compile_capturing_config(
    monkeypatch: pytest.MonkeyPatch, max_iterations: int | None
) -> dict[str, Any]:
    import langchain.agents as _la

    configs: list[dict[str, Any]] = []

    class _Runnable:
        def with_config(self, **kwargs: Any) -> Any:
            configs.append(kwargs)
            return self

    monkeypatch.setattr(_la, "create_agent", lambda **_k: _Runnable())
    ctx = CompileContext(
        service_ws=None,
        user_ws=None,
        model="databricks-claude-sonnet-4-6",
        headers=None,
    )
    agent = LlmAgent(
        tools=[],
        name="cap",
        instructions="Help.",
        max_iterations=max_iterations,
    )
    _compile_llm_agent(agent, ctx)
    assert configs, "expected with_config to be called when max_iterations is set"
    return configs[0]


def test_max_iterations_zero_sets_recursion_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#632: ``max_iterations=0`` is an explicit cap → recursion_limit=1."""
    cfg = _compile_capturing_config(monkeypatch, max_iterations=0)
    assert cfg.get("recursion_limit") == 1


def test_max_iterations_positive_sets_scaled_recursion_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _compile_capturing_config(monkeypatch, max_iterations=5)
    assert cfg.get("recursion_limit") == 5 * 2 + 1


def test_max_iterations_none_does_not_set_recursion_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None means apx adds no cap (LangGraph default) — only skip when unset."""
    import langchain.agents as _la

    configs: list[dict[str, Any]] = []

    class _Runnable:
        def with_config(self, **kwargs: Any) -> Any:
            configs.append(kwargs)
            return self

    monkeypatch.setattr(_la, "create_agent", lambda **_k: _Runnable())
    ctx = CompileContext(
        service_ws=None,
        user_ws=None,
        model="databricks-claude-sonnet-4-6",
        headers=None,
    )
    _compile_llm_agent(
        LlmAgent(tools=[], name="uncapped", instructions="Help.", max_iterations=None),
        ctx,
    )
    # No max_iterations → with_config may still run for callbacks; recursion_limit
    # must not be present from the max_iter branch.
    for cfg in configs:
        assert "recursion_limit" not in cfg
