import asyncio
import logging
import os
import queue
import threading
from contextvars import copy_context
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
from databricks.sdk import WorkspaceClient
from databricks_tools_core.auth import set_databricks_auth, clear_databricks_auth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from mcp_loader import get_mcp_servers
from system_prompt import get_system_prompt

logger = logging.getLogger(__name__)
app = FastAPI()


def _collect_result(user_message: str, options: ClaudeAgentOptions, q: queue.Queue, ctx) -> None:
    """Run query() in a fresh event loop thread and put (text, session_id) on the queue.

    Must run in a thread because claude-agent-sdk's subprocess transport
    is incompatible with uvicorn's running event loop (issue #462).
    Uses copy_context() to propagate Databricks auth context vars into the thread.
    """
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def go():
            collected_text = ""
            new_session_id = None

            async def prompt_gen():
                yield {"type": "user", "message": {"role": "user", "content": user_message}}

            async for msg in query(prompt=prompt_gen(), options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            collected_text = block.text
                elif isinstance(msg, ResultMessage):
                    new_session_id = msg.session_id

            q.put(("done", (collected_text, new_session_id)))

        try:
            loop.run_until_complete(go())
        except Exception as exc:
            q.put(("error", exc))
        finally:
            loop.close()

    ctx.run(run)


def _get_from_queue(q: queue.Queue) -> tuple:
    try:
        return q.get(timeout=300)
    except queue.Empty:
        return ("timeout", None)


@app.post("/responses")
async def responses(request: Request):
    body = await request.json()
    messages = body.get("input", [])
    session_id = body.get("session_id")

    if not messages:
        raise HTTPException(status_code=400, detail="input must not be empty")

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")

    if not host:
        raise HTTPException(status_code=500, detail="DATABRICKS_HOST not configured")

    ws = WorkspaceClient(host=host, token=token)
    user_email = await asyncio.to_thread(lambda: ws.current_user.me().user_name)

    set_databricks_auth(host, token)
    try:
        servers, tool_names = get_mcp_servers()
        user_message = messages[-1]["content"]

        options = ClaudeAgentOptions(
            cwd="/tmp",
            allowed_tools=["Write"] + tool_names,
            permission_mode="bypassPermissions",
            resume=session_id,
            mcp_servers=servers,
            system_prompt=get_system_prompt(user_email),
            env={
                "ANTHROPIC_API_KEY": token,
                "ANTHROPIC_BASE_URL": f"{host}/serving-endpoints/anthropic",
                "ANTHROPIC_MODEL": "databricks-claude-sonnet-4-6",
                "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-disable-beta-headers: true",
            },
        )

        q: queue.Queue = queue.Queue()
        ctx = copy_context()
        thread = threading.Thread(
            target=_collect_result,
            args=(user_message, options, q, ctx),
            daemon=True,
        )
        thread.start()

        loop = asyncio.get_event_loop()
        msg_type, payload = await loop.run_in_executor(None, lambda: _get_from_queue(q))

        if msg_type == "timeout":
            raise HTTPException(status_code=504, detail="Agent timed out after 5 minutes")
        if msg_type == "error":
            logger.error("Agent error: %s", payload)
            raise HTTPException(status_code=500, detail=str(payload))

        text, new_session_id = payload
        return {
            "output": [{"type": "message", "content": [{"text": text or ""}]}],
            "session_id": new_session_id or session_id,
        }
    finally:
        clear_databricks_auth()


# Serve the React frontend
_here = Path(__file__).resolve()
_candidates = [
    Path.cwd() / "client" / "dist",
    _here.parent / "client" / "dist",
]
_CLIENT_DIST = next((c for c in _candidates if c.exists()), None)

if _CLIENT_DIST is not None:
    @app.get("/", include_in_schema=False)
    def spa_index():
        return FileResponse(str(_CLIENT_DIST / "index.html"))

    @app.get("/assets/{path:path}", include_in_schema=False)
    def spa_assets(path: str):
        asset = _CLIENT_DIST / "assets" / path
        return FileResponse(str(asset) if asset.is_file() else str(_CLIENT_DIST / "index.html"))
