"""foundation_model_tool — call a Foundation Model serving endpoint as a tool.

Use this when one agent should be able to consult a different model
mid-conversation — e.g. a router agent asking a specialist model a hard
question, or a small fast model deferring to a larger reasoning model for
specific subtasks. The endpoint shows up in the declared resources list,
so the platform mints a scoped token and governance / cost tracking flow
through Mosaic AI Gateway.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly at function definition time
and ``get_type_hints()`` in ``_inspection.py`` sees the real ``Annotated[...]``
type, not a string.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def foundation_model_tool(
    endpoint_name: str,
    *,
    system_prompt: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    name: str = "ask_model",
    description: str | None = None,
) -> Any:
    """Return a tool that asks a Foundation Model API endpoint a question.

    The LLM sees one parameter — ``prompt: str`` — and the endpoint and
    sampling parameters are fixed at factory time. Auth runs as the calling
    user via the standard OBO path, so the endpoint's per-user access
    policies and AI Gateway routing apply.

    Usage — fan a question out to a heavier reasoning model::

        from apx_agent import Agent, foundation_model_tool

        agent = Agent(
            instructions="Route hard reasoning questions to the specialist.",
            tools=[
                foundation_model_tool(
                    endpoint_name="databricks-claude-opus-4-7",
                    name="ask_opus",
                    description="Ask the deep-reasoning specialist a complex question.",
                    system_prompt="You are a senior data engineer. Be terse.",
                ),
            ],
        )

    Args:
        endpoint_name: Databricks Model Serving endpoint name (e.g.
            ``"databricks-claude-sonnet-4-6"``, or a custom endpoint).
        system_prompt: Optional system prompt prepended to every call.
            Fixed at factory time. For per-call system prompts, expose a
            custom tool.
        temperature: Optional sampling temperature.
        max_tokens: Optional response length cap.
        name: Tool name shown to the calling LLM. Defaults to ``"ask_model"``.
        description: Tool description shown to the calling LLM. When
            omitted, generated from the endpoint name.
    """
    from ._defaults import UserClientDependency
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    _desc = description or (
        f"Ask the `{endpoint_name}` foundation model a question. "
        f"Pass a natural-language prompt and get the model's response back."
    )

    async def _ask_model(prompt: str, ws: UserClientDependency) -> str:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

        messages: list[ChatMessage] = []
        if system_prompt:
            messages.append(ChatMessage(role=ChatMessageRole.SYSTEM, content=system_prompt))
        messages.append(ChatMessage(role=ChatMessageRole.USER, content=prompt))

        kwargs: dict[str, Any] = {"name": endpoint_name, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            response = ws.serving_endpoints.query(**kwargs)
        except Exception as e:
            logger.warning("Foundation model query failed on %s: %s", endpoint_name, e)
            return f"Model query failed: {e}"

        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        if message is None:
            return ""
        return getattr(message, "content", "") or ""

    return build_tool(
        _ask_model,
        name=name,
        description=_desc,
        resources=[ResourceSpec("serving_endpoint", endpoint_name)],
    )
