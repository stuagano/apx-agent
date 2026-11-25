"""foundation_model_tool — ask a Unity Gateway model service as a tool.

Use this when one agent should be able to consult a different model
mid-conversation — e.g. a router agent asking a specialist model a hard
question, or a small fast model deferring to a larger reasoning model for
specific subtasks. Every call goes to Unity Gateway
(``{host}/ai-gateway/mlflow/v1``) as the App service principal, and the
model service shows up in the declared resources as ``MODEL_SERVICE`` /
``EXECUTE``, so deploy grants that principal access.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``ClientDependency`` is resolved eagerly at function definition time
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
    """Return a tool that asks a Unity Gateway model service a question.

    The LLM sees one parameter — ``prompt: str`` — and the model and
    sampling parameters are fixed at factory time. The tool posts to
    ``{host}/ai-gateway/mlflow/v1`` as the App service principal and declares
    an Apps ``MODEL_SERVICE`` resource with ``EXECUTE``.

    Usage — fan a question out to a heavier reasoning model::

        from apx_agent import Agent, foundation_model_tool

        agent = Agent(
            instructions="Route hard reasoning questions to the specialist.",
            tools=[
                foundation_model_tool(
                    "system.ai.claude-opus-4-7",
                    name="ask_opus",
                    description="Ask the deep-reasoning specialist a complex question.",
                    system_prompt="You are a senior data engineer. Be terse.",
                ),
            ],
        )

    A pay-per-token ``databricks-<id>`` name is rewritten to
    ``system.ai.<id>``, the same rewrite ``get_llm`` applies to the chat
    model, so ``foundation_model_tool("databricks-claude-opus-4-7")`` calls
    ``system.ai.claude-opus-4-7``.

    Args:
        endpoint_name: Unity Gateway model service ``catalog.schema.service``
            (e.g. ``"system.ai.claude-sonnet-4-5"``), or a ``databricks-<id>``
            foundation-model name. Any other name raises ``ValueError``: put a
            custom model behind a Unity Gateway model service and pass its
            three-part name.
        system_prompt: Optional system prompt prepended to every call.
            Fixed at factory time. For per-call system prompts, expose a
            custom tool.
        temperature: Optional sampling temperature.
        max_tokens: Optional response length cap.
        name: Tool name shown to the calling LLM. Defaults to ``"ask_model"``.
        description: Tool description shown to the calling LLM. When
            omitted, generated from the model service name.
    """
    from ._defaults import ClientDependency
    from ._llm import AI_GATEWAY_CHAT_PATH, explain_model_service_error, model_service_name
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    service = model_service_name(endpoint_name)
    if service is None:
        raise ValueError(
            f"foundation_model_tool({endpoint_name!r}): model calls go through Unity "
            "Gateway, not Model Serving endpoints. Pass a Unity Gateway model service "
            "(catalog.schema.service, e.g. system.ai.claude-sonnet-4-5) or a "
            "databricks-<id> foundation-model name. For a custom model, declare a Unity "
            "Gateway model service for it and pass that three-part name."
        )

    _desc = description or (
        f"Ask the `{service}` foundation model a question. "
        f"Pass a natural-language prompt and get the model's response back."
    )

    async def _ask_service(prompt: str, ws: ClientDependency) -> str:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
        messages.append({"role": "user", "content": prompt})
        body: dict[str, Any] = {"model": service, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        try:
            response = ws.api_client.do("POST", AI_GATEWAY_CHAT_PATH, body=body)
        except Exception as e:
            logger.warning("Unity Gateway query failed on %s: %s", service, e)
            return explain_model_service_error(service, e)
        choices = response.get("choices") if isinstance(response, dict) else None
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content")
        return content if isinstance(content, str) else ""

    return build_tool(
        _ask_service,
        name=name,
        description=_desc,
        resources=[ResourceSpec("model_service", service)],
        execution="service",
    )
