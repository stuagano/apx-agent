"""Thin proxy that translates Anthropic /v1/messages calls to Databricks chat/completions.

The claude-agent-sdk subprocess uses the Anthropic SDK which calls /v1/messages.
Databricks serving endpoints only accept /invocations (OpenAI-compatible format),
so we proxy locally and translate on the fly.

Set ANTHROPIC_BASE_URL=http://localhost:8000 in the subprocess env to route here.
The user's Databricks token is passed via the x-api-key header by the Anthropic SDK.
"""

import json
import logging
import os
import threading

import httpx
from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter()

# Singleton SDK client — uses DATABRICKS_CLIENT_ID/SECRET (M2M) in Apps,
# or DATABRICKS_TOKEN (PAT) in local dev. Either has model-serving scope.
_sdk_lock = threading.Lock()
_sdk_client: WorkspaceClient | None = None


def _get_sdk_auth_header(host: str) -> str:
    """Return 'Bearer <token>' using the app's own credentials, not the user's token."""
    global _sdk_client
    with _sdk_lock:
        if _sdk_client is None:
            _sdk_client = WorkspaceClient(host=host)
    headers = _sdk_client.config.authenticate()
    return headers.get("Authorization", "")


def _to_openai(body: dict) -> dict:
    messages = list(body.get("messages", []))

    if system := body.get("system"):
        text = system if isinstance(system, str) else " ".join(
            b.get("text", "") for b in system if b.get("type") == "text"
        )
        messages = [{"role": "system", "content": text}] + messages

    converted = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text_parts, tool_calls = [], []
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
            entry: dict = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            converted.append(entry)

        elif role == "user":
            tool_results = [b for b in content if b.get("type") == "tool_result"]
            text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            for tr in tool_results:
                tr_content = tr.get("content", "")
                if isinstance(tr_content, list):
                    tr_content = "\n".join(b.get("text", "") for b in tr_content if b.get("type") == "text")
                converted.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id", ""),
                    "content": tr_content,
                })
            if text_parts:
                converted.append({"role": "user", "content": "\n".join(text_parts)})
        else:
            converted.append({"role": role, "content": str(content)})

    result: dict = {
        "model": body.get("model", "databricks-claude-sonnet-4-6"),
        "max_tokens": body.get("max_tokens", 4096),
        "messages": converted,
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in body.get("tools", [])
    ]
    if tools:
        result["tools"] = tools
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    return result


def _to_anthropic(original: dict, oai: dict) -> dict:
    choice = oai.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish_reason = choice.get("finish_reason", "stop")

    content = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls", []):
        try:
            input_data = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            input_data = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": tc.get("function", {}).get("name", ""),
            "input": input_data,
        })

    stop_reason = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(
        finish_reason, "end_turn"
    )
    usage = oai.get("usage", {})
    return {
        "id": oai.get("id", "msg_proxy"),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": original.get("model", ""),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


@router.post("/v1/messages")
async def anthropic_proxy(request: Request) -> dict:
    """Translate Anthropic API calls to Databricks chat/completions."""
    if not request.headers.get("x-api-key", "").strip():
        raise HTTPException(status_code=401, detail="x-api-key required")

    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if not host:
        raise HTTPException(status_code=500, detail="DATABRICKS_HOST not configured")
    if not host.startswith(("http://", "https://")):
        host = f"https://{host}"

    body = await request.json()
    openai_body = _to_openai(body)

    import asyncio
    auth_header = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _get_sdk_auth_header(host)
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{host}/serving-endpoints/chat/completions",
            headers={"Authorization": auth_header, "Content-Type": "application/json"},
            json=openai_body,
        )

    if not resp.is_success:
        logger.error("Databricks proxy error %s: %s", resp.status_code, resp.text[:200])
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return _to_anthropic(body, resp.json())
