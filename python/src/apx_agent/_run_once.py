"""run_once — invoke an agent in batch (no HTTP, no FastAPI lifecycle).

The third deployment shape, complementing Model Serving (``log_agent`` +
``databricks.agents.deploy``) and Apps hosting (``create_app``). Designed
for Databricks Job / cron / scheduled batch contexts where the agent
needs to run once with a known prompt and return its final text — no
incoming HTTP request, no serving endpoint, just compile + invoke.

Today, batch users typically hand-roll the LLM loop directly against
the Model Serving endpoint (skipping the framework entirely) because no
clean batch primitive exists. That reimplements 100+ lines that the
framework already owns. ``run_once`` collapses it to a single call.

Auth: uses the framework's standard ``_make_workspace_client``
resolution. In a Databricks Job, set ``DATABRICKS_CLIENT_ID`` +
``DATABRICKS_CLIENT_SECRET`` as job env vars — the framework
auto-detects OAuth M2M. For local dev, ``DATABRICKS_TOKEN`` works.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._agents import BaseAgent

# Top-level import so tests can patch and so import failures surface as
# friendlier errors when the eval extra (mlflow) isn't installed. The
# function itself raises a clearer error when this is None.
try:
    from ._chat_agent import compile_to_chat_agent
except ImportError:  # pragma: no cover — defensive
    compile_to_chat_agent = None  # type: ignore[assignment]

from ._inspection import _load_agent_config


def run_once(
    agent: "BaseAgent",
    prompt: str,
    *,
    model: str | None = None,
    session_id: str | None = None,
    custom_inputs: dict[str, Any] | None = None,
) -> str:
    """Invoke an agent once and return its final assistant text.

    Args:
        agent: Any ``BaseAgent`` (``LlmAgent``, ``SequentialAgent``, ...).
        prompt: User prompt string. For richer inputs (system + user, or
            a multi-turn history) call ``compile_to_chat_agent(agent)``
            directly and pass a custom ``messages`` list to ``predict``.
        model: LLM endpoint. Defaults to ``[tool.apx.agent].model`` in
            ``pyproject.toml`` (resolved via ``_load_agent_config``).
        session_id: Optional session ID — when the agent's ChatAgent has a
            session store configured, loads + persists multi-turn history.
        custom_inputs: Additional fields forwarded to ``predict()``.
            ``session_id`` is merged in if both are provided.

    Returns:
        The final assistant message text. Empty string when the agent
        produced no assistant message — rare, usually indicates the
        agent loop hit ``max_iterations`` before producing output.

    Raises:
        ValueError: when ``model`` is unset and pyproject.toml doesn't
            configure ``[tool.apx.agent].model``.

    Example::

        # Databricks Job entrypoint, scheduled via databricks.yml
        from apx_agent import run_once
        from my_agent import agent
        from my_outputs import post_to_slack

        def main() -> int:
            result = run_once(agent, "Run today's scan.")
            post_to_slack(result)
            return 0
    """
    if compile_to_chat_agent is None:
        raise ImportError(
            "run_once requires the eval extra (mlflow). "
            "Install with: pip install 'apx-agent[eval]'"
        )

    effective_model = model
    if effective_model is None:
        cfg = _load_agent_config()
        effective_model = cfg.model if cfg is not None else None
    if not effective_model:
        raise ValueError(
            "run_once requires a model. Pass model= explicitly or set "
            "[tool.apx.agent].model in pyproject.toml."
        )

    chat_agent = compile_to_chat_agent(agent, model=effective_model)

    from mlflow.types.agent import ChatAgentMessage
    messages = [ChatAgentMessage(role="user", content=prompt)]

    inputs = dict(custom_inputs or {})
    if session_id is not None:
        inputs.setdefault("session_id", session_id)

    response = chat_agent.predict(
        messages=messages,
        custom_inputs=inputs or None,
    )

    response_messages = getattr(response, "messages", []) or []
    for msg in reversed(response_messages):
        if getattr(msg, "role", None) == "assistant":
            return getattr(msg, "content", "") or ""
    return ""
