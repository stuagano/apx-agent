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

from ._obo import reauthorize_hint

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

    Unity Gateway model service — pass a three-part name instead of an
    endpoint id. The tool posts to ``{host}/ai-gateway/mlflow/v1`` as the App
    service principal and declares an Apps ``uc_securable`` /
    ``MODEL_SERVICE`` resource with ``EXECUTE``, so deploy grants that
    principal access::

        foundation_model_tool("system.ai.claude-sonnet-4-5", name="ask_claude")

    Args:
        endpoint_name: Databricks Model Serving endpoint name (e.g.
            ``"databricks-claude-sonnet-4-6"``, or a custom endpoint), or a
            Unity Gateway model service ``catalog.schema.service`` (e.g.
            ``"system.ai.claude-sonnet-4-5"``). Endpoint names cannot contain
            ``.``, so a dotted name is always a model service.
        system_prompt: Optional system prompt prepended to every call.
            Fixed at factory time. For per-call system prompts, expose a
            custom tool.
        temperature: Optional sampling temperature.
        max_tokens: Optional response length cap.
        name: Tool name shown to the calling LLM. Defaults to ``"ask_model"``.
        description: Tool description shown to the calling LLM. When
            omitted, generated from the endpoint name.
    """
    from ._defaults import ClientDependency, UserClientDependency
    from ._llm import AI_GATEWAY_CHAT_PATH, explain_model_service_error
    from ._resources import ResourceSpec
    from ._tool_factory import build_tool

    _desc = description or (
        f"Ask the `{endpoint_name}` foundation model a question. "
        f"Pass a natural-language prompt and get the model's response back."
    )

    if "." in endpoint_name:
        if len(endpoint_name.split(".")) != 3 or not all(endpoint_name.split(".")):
            raise ValueError(
                f"foundation_model_tool({endpoint_name!r}): a Unity Gateway model service "
                "is catalog.schema.service; a Model Serving endpoint name has no dots."
            )
        service = endpoint_name

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
            hint = reauthorize_hint(e)
            if hint is not None:
                return str(hint)
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
