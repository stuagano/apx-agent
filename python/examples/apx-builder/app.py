import asyncio
import json
import logging
import os
import queue
import threading
from contextvars import copy_context
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock
from databricks.sdk import WorkspaceClient
from databricks_tools_core.auth import set_databricks_auth, clear_databricks_auth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from anthropic_proxy import router as proxy_router
from mcp_loader import get_mcp_servers
from system_prompt import get_build_prompt

logger = logging.getLogger(__name__)
app = FastAPI()
app.include_router(proxy_router)

# ---------------------------------------------------------------------------
# Discovery state — in-memory, keyed by session_id.
# The discovery phase never calls the LLM: we return scripted questions
# directly and collect answers until all 5 are filled.
# ---------------------------------------------------------------------------

QUESTIONS = [
    "What should your agent do?",
    "Which tables or data sources should it use?",
    "Should it connect to any Genie spaces?",
    "Should it be able to answer questions about data lineage?",
    "What should we call this agent? For example, if it handles sales questions, I'd call it sales-assistant.",
]

ANSWER_KEYS = ["use_case", "tables", "genie", "lineage", "name"]


@dataclass
class DiscoverySession:
    answers: dict = field(default_factory=dict)
    question_index: int = 0           # 0-4: which question to ask next
    build_confirmed: bool = False     # True after payload summary has been shown
    build_session_id: Optional[str] = None  # SDK session id once build starts


def _format_build_summary(answers: dict) -> str:
    name = answers.get("name", "my-agent")
    use_case = answers.get("use_case", "")
    tables = answers.get("tables", "none")
    genie = answers.get("genie", "none")
    lineage_raw = answers.get("lineage", "no").lower()
    lineage = "yes" if lineage_raw in ("yes", "y") else "no"

    lines = [f"Got it — building **mcp-{name}** now. This takes about 2 minutes.\n"]
    lines.append(f"**What it does:** {use_case}")
    lines.append(f"**Tables:** {tables}")
    if genie.lower() not in ("none", "no", "n/a", "skip", ""):
        lines.append(f"**Genie spaces:** {genie}")
    lines.append(f"**Lineage:** {lineage}")
    return "\n".join(lines)


# session_id (str) → DiscoverySession
_sessions: dict[str, DiscoverySession] = {}
_sessions_lock = threading.Lock()


def _get_session(session_id: str) -> DiscoverySession:
    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = DiscoverySession()
        return _sessions[session_id]


def _next_question(ds: DiscoverySession) -> Optional[str]:
    """Return the next unanswered question, or None if all answered."""
    if ds.question_index < len(QUESTIONS):
        return QUESTIONS[ds.question_index]
    return None


# ---------------------------------------------------------------------------
# Phase 2: build via Claude Code SDK
# ---------------------------------------------------------------------------

def _collect_result(user_message: str, options: ClaudeAgentOptions, q: queue.Queue, ctx) -> None:
    """Run query() in a fresh event loop thread and put (text, session_id) on the queue."""
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
                            collected_text += block.text
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


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@app.post("/responses")
async def responses(request: Request):
    body = await request.json()
    messages = body.get("input", [])
    session_id = body.get("session_id")

    if not messages:
        raise HTTPException(status_code=400, detail="input must not be empty")

    user_message = messages[-1].get("content")
    if not user_message:
        raise HTTPException(status_code=400, detail="last input message must have non-empty content")

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.headers.get("X-Forwarded-Access-Token", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Authorization token required")

    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if not host:
        raise HTTPException(status_code=500, detail="DATABRICKS_HOST not configured")

    # Resolve session_id — create one on first message so we can track state
    if not session_id:
        import uuid
        session_id = str(uuid.uuid4())

    ds = _get_session(session_id)

    # ------------------------------------------------------------------
    # Phase 1: Discovery — return scripted questions without calling LLM
    #
    # State machine:
    #   question_index=0: no question asked yet → ask Q1, advance to 1
    #   question_index=1: Q1 asked, waiting for answer → record as use_case, ask Q2, advance to 2
    #   ...
    #   question_index=4: Q4 asked, waiting for answer → record as lineage, ask Q5, advance to 5
    #   question_index=5: Q5 asked, waiting for answer → record as name, fall through to build
    # ------------------------------------------------------------------
    if ds.question_index <= len(QUESTIONS):
        # Record answer to the previous question (if any question was asked)
        if ds.question_index > 0:
            key = ANSWER_KEYS[ds.question_index - 1]
            ds.answers[key] = user_message

        # If we've now collected all 5 answers, show payload confirmation first.
        # The frontend will auto-trigger the actual LLM build on the next turn.
        if ds.question_index == len(QUESTIONS):
            if not ds.build_confirmed:
                ds.build_confirmed = True
                return {
                    "output": [{"type": "message", "content": [{"text": _format_build_summary(ds.answers)}]}],
                    "session_id": session_id,
                    "build_pending": True,
                }
            # build_confirmed == True: fall through to Phase 2 LLM call
        else:
            # Ask the next question
            next_q = QUESTIONS[ds.question_index]
            ds.question_index += 1
            return {
                "output": [{"type": "message", "content": [{"text": next_q}]}],
                "session_id": session_id,
            }

    # ------------------------------------------------------------------
    # Phase 2: Build — delegate to Claude Code SDK
    # ------------------------------------------------------------------
    ws = WorkspaceClient(host=host, token=token, auth_type="pat")
    user_email = await asyncio.to_thread(lambda: ws.current_user.me().user_name)

    set_databricks_auth(host, token, force_token=True)
    try:
        servers, _ = get_mcp_servers()

        options = ClaudeAgentOptions(
            cwd="/tmp",
            allowed_tools=[
                "Read",
                "Write",
                "mcp__apx__manage_workspace_files",
                "mcp__apx__create_and_deploy_app",
                "mcp__apx__get_app_status",
            ],
            permission_mode="bypassPermissions",
            resume=ds.build_session_id,
            mcp_servers=servers,
            system_prompt=get_build_prompt(user_email),
            env={
                "ANTHROPIC_API_KEY": token,
                "ANTHROPIC_BASE_URL": "http://localhost:8000",
                "ANTHROPIC_MODEL": "databricks-claude-sonnet-4-6",
                "DATABRICKS_HOST": host,
            },
        )

        # On first build call, send a complete task description with all answers.
        # The "ALL SPECIFICATIONS COLLECTED" header prevents the LLM from trying
        # to ask discovery questions — it makes clear these are given inputs, not prompts.
        if ds.build_session_id is None:
            build_message = (
                "ALL SPECIFICATIONS COLLECTED — proceed directly to build. Do not ask any questions.\n\n"
                f"Use case: {ds.answers.get('use_case', '')}\n"
                f"Tables: {ds.answers.get('tables', 'none')}\n"
                f"Genie spaces: {ds.answers.get('genie', 'none')}\n"
                f"Lineage: {ds.answers.get('lineage', 'no')}\n"
                f"App name: {ds.answers.get('name', 'my-agent')}\n\n"
                "Follow the Phase 2 build instructions in your system prompt."
            )
        else:
            build_message = user_message

        q: queue.Queue = queue.Queue()
        ctx = copy_context()
        thread = threading.Thread(
            target=_collect_result,
            args=(build_message, options, q, ctx),
            daemon=True,
        )
        thread.start()

        msg_type, payload = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _get_from_queue(q)
        )

        if msg_type == "timeout":
            raise HTTPException(status_code=504, detail="Agent timed out after 5 minutes")
        if msg_type == "error":
            logger.error("Agent error: %s", payload)
            raise HTTPException(status_code=500, detail=str(payload))

        text, new_sdk_session_id = payload
        if new_sdk_session_id:
            ds.build_session_id = new_sdk_session_id

        return {
            "output": [{"type": "message", "content": [{"text": text or ""}]}],
            "session_id": session_id,
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
