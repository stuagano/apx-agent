"""genie_tool — wrap a Genie space as a registered apx-agent tool.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that ``UserClientDependency`` is resolved eagerly at function definition time and
``get_type_hints()`` in _inspection.py sees the real Annotated[...] type, not a string.
"""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def genie_tool(
    space_id: str,
    *,
    name: str = "ask_genie",
    description: str | None = None,
) -> Any:
    """Return a tool function that queries a Genie space by natural-language conversation.

    The returned callable is a normal apx-agent tool: register it directly in
    ``Agent(tools=[...])``.  The LLM sees one parameter — ``question: str`` —
    and the workspace client is injected automatically from the incoming request.

    Usage::

        from apx_agent import Agent, genie_tool

        agent = Agent(
            tools=[genie_tool("abc123", description="Answer sales data questions")],
        )

    Args:
        space_id: Genie space ID (the UUID from the Genie space URL).
        name: Tool name shown to the LLM. Defaults to ``"ask_genie"``.
        description: Tool description shown to the LLM.
    """
    # Import here so that the annotation below is the actual Annotated[...] type
    # (eagerly evaluated — no deferred string annotation in scope).
    from ._defaults import UserClientDependency

    _desc = (
        description
        or f"Ask a natural-language question to the Genie space and receive an answer. "
        f"Use this for data questions that Genie can answer via SQL. (space_id={space_id})"
    )

    async def _ask_genie(question: str, ws: UserClientDependency) -> str:  # type: ignore[valid-type]
        """Placeholder doc — overwritten below."""
        conv_resp = ws.api_client.do(
            "POST",
            f"/api/2.0/genie/spaces/{space_id}/start_conversation",
            body={"content": question},
        )
        genie_conv_id = conv_resp.get("conversation_id", "")
        message_id = conv_resp.get("message_id", "")

        msg_resp: dict[str, Any] = {}
        for _ in range(30):
            msg_resp = ws.api_client.do(
                "GET",
                f"/api/2.0/genie/spaces/{space_id}/conversations/{genie_conv_id}/messages/{message_id}",
            )
            status = msg_resp.get("status", "")
            if status == "COMPLETED":
                break
            if status in ("FAILED", "CANCELLED"):
                logger.warning("Genie query %s for space %s", status.lower(), space_id)
                return f"Genie query {status.lower()}."
            await asyncio.sleep(2)

        for att in msg_resp.get("attachments", []):
            text_block = att.get("text", {})
            if text_block.get("content"):
                return str(text_block["content"])

        return ""

    _ask_genie.__name__ = name
    _ask_genie.__qualname__ = name
    _ask_genie.__doc__ = _desc
    return _ask_genie
