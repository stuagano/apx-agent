"""Proxy chat messages to agents based on their source type."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from typing import TYPE_CHECKING, Any

import httpx

from .models import ChatRequest, ChatResponse, HubAgent

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


async def proxy_chat(
    request: ChatRequest,
    agent: HubAgent,
    headers: dict[str, str],
    ws: "WorkspaceClient | None" = None,
) -> ChatResponse:
    """Route a chat message to the appropriate backend."""
    conversation_id = request.conversation_id or str(uuid.uuid4())

    if agent.source == "apx":
        return await _chat_apx(request, agent, headers, conversation_id)
    elif agent.source == "serving_endpoint":
        if ws is None:
            raise ValueError("WorkspaceClient required for serving endpoint chat")
        return await _chat_serving_endpoint(request, agent, ws, conversation_id)
    elif agent.source == "genie_space":
        if ws is None:
            raise ValueError("WorkspaceClient required for Genie space chat")
        return await _chat_genie_space(request, agent, ws, conversation_id)
    else:
        raise ValueError(f"Unknown source: {agent.source}")


async def _chat_apx(
    request: ChatRequest,
    agent: HubAgent,
    headers: dict[str, str],
    conversation_id: str,
) -> ChatResponse:
    """Send message to an apx-agent app via /responses."""
    if not agent.url:
        raise ValueError(f"Agent '{agent.name}' has no URL configured")

    url = f"{agent.url.rstrip('/')}/responses"
    payload: dict[str, Any] = {
        "input": [{"role": "user", "content": request.message}],
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        _json_result = resp.json()
        data = await _json_result if inspect.isawaitable(_json_result) else _json_result

    text = data.get("output_text", "")
    if not text:
        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text", "")
                    break
            if text:
                break

    return ChatResponse(agent_id=request.agent_id, message=text, conversation_id=conversation_id)


async def _chat_serving_endpoint(
    request: ChatRequest,
    agent: HubAgent,
    ws: "WorkspaceClient",
    conversation_id: str,
) -> ChatResponse:
    """Send message to a serving endpoint via Databricks SDK."""
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

    response = ws.serving_endpoints.query(
        name=agent.url,
        messages=[ChatMessage(role=ChatMessageRole.USER, content=request.message)],
    )
    text = response.choices[0].message.content if response.choices else ""
    return ChatResponse(agent_id=request.agent_id, message=text, conversation_id=conversation_id)


async def _chat_genie_space(
    request: ChatRequest,
    agent: HubAgent,
    ws: "WorkspaceClient",
    conversation_id: str,
) -> ChatResponse:
    """Send message to a Genie space via conversation API.

    Genie responses are asynchronous — this polls the message status until
    COMPLETED (or up to 60 seconds), then extracts text from attachments.
    """
    space_id = agent.metadata.get("space_id", "")

    conv_resp = ws.api_client.do(
        "POST",
        f"/api/2.0/genie/spaces/{space_id}/start_conversation",
        body={"content": request.message},
    )
    genie_conv_id = conv_resp.get("conversation_id", "")
    message_id = conv_resp.get("message_id", "")

    # Poll until the message is ready (Genie processes queries asynchronously)
    msg_resp: dict = {}
    for _ in range(30):
        msg_resp = ws.api_client.do(
            "GET",
            f"/api/2.0/genie/spaces/{space_id}/conversations/{genie_conv_id}/messages/{message_id}",
        )
        status = msg_resp.get("status", "")
        if status == "COMPLETED":
            break
        if status in ("FAILED", "CANCELLED"):
            return ChatResponse(
                agent_id=request.agent_id,
                message=f"Genie query {status.lower()}.",
                conversation_id=f"{space_id}:{genie_conv_id}",
            )
        await asyncio.sleep(2)

    attachments = msg_resp.get("attachments", [])
    text = ""
    for att in attachments:
        text_block = att.get("text", {})
        if text_block.get("content"):
            text = text_block["content"]
            break

    return ChatResponse(
        agent_id=request.agent_id,
        message=text,
        conversation_id=f"{space_id}:{genie_conv_id}",
    )
