"""sandbox_tool — run model-generated Python or shell in a Databricks Sandbox (#1266).

A Databricks Sandbox is an isolated serverless Linux environment (4 vCPUs, 8 GB
RAM, Beta). The tool sends the code to the Sandbox API and returns its exit
code, stdout and stderr; nothing the model writes runs in the App process.

The Sandbox REST API is called through the workspace client's ``api_client``
(the same paths ``ws.sandbox`` uses in databricks-sdk >= 0.146), so the tool
works at the package's databricks-sdk floor without the ``[sandbox]`` extra.

Annotations are intentionally NOT deferred (no ``from __future__ import annotations``)
so that the client dependency is resolved eagerly and ``get_type_hints()`` sees
the real Annotated[...] type.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Literal

from databricks.sdk.errors import NotFound

from ._errors import ToolError
from ._mlflow_tracing import emit_progress
from ._obo import reauthorize_hint

logger = logging.getLogger(__name__)

SANDBOX_SERVICE = "system.ai.sandbox"
SANDBOXES_PATH = "/api/2.0/sandboxes"
EXEC_SYNC_PATH = "/api/2.0/sandbox-exec/sandboxes/{sandbox_id}/exec-sync"
ENABLE_SANDBOX = (
    "A workspace admin enables Databricks Sandbox on the workspace Previews page "
    "(Beta; the workspace must be in a supported serverless region)."
)

_COMMANDS = {"python": "python3", "shell": "/bin/bash"}
_POLL_S = 1.0
_BACKSTOP_S = 10.0


def preview_off(exc: Exception) -> bool:
    """Whether a Sandbox API error reads as the workspace preview being off."""
    code = getattr(exc, "error_code", None)
    msg = str(exc).lower()
    return code in {"FEATURE_DISABLED", "NOT_IMPLEMENTED", "ENDPOINT_NOT_FOUND"} or "not enabled" in msg or "preview" in msg


def _tool_error(exc: Exception) -> ToolError:
    hint = reauthorize_hint(exc)
    if hint is not None:
        return ToolError(str(hint))
    if preview_off(exc):
        return ToolError(f"Databricks Sandbox is not available: {exc}. {ENABLE_SANDBOX}")
    return ToolError(f"Sandbox request failed: {exc}")


def _headers(ws: Any) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if ws.config.workspace_id:
        headers["X-Databricks-Workspace-Id"] = str(ws.config.workspace_id)
    return headers


def _state(sandbox: dict[str, Any]) -> str | None:
    status = sandbox.get("status")
    return status.get("state") if isinstance(status, dict) else None


def _running(ws: Any, sandbox_id: str, deadline: float) -> None:
    """Start ``sandbox_id`` if it is stopped and wait until it is running or ``deadline`` passes."""
    path = f"{SANDBOXES_PATH}/{sandbox_id}"
    while True:
        state = _state(ws.api_client.do("GET", path, headers=_headers(ws)))
        if state == "SANDBOX_STATE_RUNNING":
            return
        if state == "SANDBOX_STATE_STOPPED":
            ws.api_client.do("POST", f"{path}/start", body={}, headers=_headers(ws))
        if time.monotonic() >= deadline:
            raise TimeoutError(f"sandbox {sandbox_id} is still {state}")
        time.sleep(_POLL_S)


def _create(ws: Any, sandbox_id: str) -> None:
    ws.api_client.do("POST", SANDBOXES_PATH, query={"sandbox_id": sandbox_id}, body={},
                     headers=_headers(ws))


def _exec(ws: Any, sandbox_id: str, language: str, code: str, deadline: float) -> dict[str, Any]:
    remaining = max(1, int(deadline - time.monotonic()))
    return ws.api_client.do(
        "POST",
        EXEC_SYNC_PATH.format(sandbox_id=sandbox_id),
        body={"cmd": _COMMANDS[language], "args": ["-c", code], "execution_timeout": f"{remaining}s"},
        headers=_headers(ws),
    )


def _run(ws: Any, sandbox_id: str | None, language: str, code: str, deadline: float) -> dict[str, Any]:
    """Run ``code`` in the named sandbox (created on first use), or in a throwaway one."""
    if sandbox_id is not None:
        try:
            emit_progress(f"Waking Databricks Sandbox {sandbox_id}", kind="sandbox")
            _running(ws, sandbox_id, deadline)
        except NotFound:
            emit_progress(f"Creating Databricks Sandbox {sandbox_id}", kind="sandbox")
            _create(ws, sandbox_id)
            _running(ws, sandbox_id, deadline)
        emit_progress(f"Running {language} in the sandbox", kind="sandbox")
        return _exec(ws, sandbox_id, language, code, deadline)

    ephemeral = f"apx-{uuid.uuid4().hex[:12]}"
    emit_progress("Creating a Databricks Sandbox", kind="sandbox")
    _create(ws, ephemeral)
    try:
        _running(ws, ephemeral, deadline)
        emit_progress(f"Running {language} in the sandbox", kind="sandbox")
        return _exec(ws, ephemeral, language, code, deadline)
    finally:
        try:
            ws.api_client.do("DELETE", f"{SANDBOXES_PATH}/{ephemeral}", headers=_headers(ws))
        except Exception as exc:
            logger.warning("Could not delete Databricks Sandbox %s: %s", ephemeral, exc)


def _result(response: dict[str, Any], wait_s: float) -> dict[str, Any]:
    status = response.get("status")
    if status == "EXECUTE_COMMAND_STATUS_TIMED_OUT":
        raise ToolError(f"Sandbox command did not finish within {wait_s:.0f}s and was terminated.")
    if status != "EXECUTE_COMMAND_STATUS_COMPLETED":
        raise ToolError(f"Sandbox command did not run (status: {status}).")
    return {
        "exit_code": response.get("exit_code"),
        "stdout": response.get("stdout") or "",
        "stderr": response.get("stderr") or "",
        "truncated": bool(response.get("truncated")),
    }


def sandbox_tool(
    sandbox_id: str | None = None,
    *,
    auth: Literal["user", "app"] = "user",
    name: str = "run_in_sandbox",
    description: str | None = None,
    wait_s: float = 300.0,
) -> Any:
    """Return a tool that runs Python or shell code in a Databricks Sandbox.

    The model passes ``code`` and ``language`` (``"python"`` or ``"shell"``);
    the tool returns ``exit_code``, ``stdout``, ``stderr`` and ``truncated``.
    A non-zero exit code is a result, so the model can read the traceback and
    retry. The code runs in the sandbox, never in the App process.

    Databricks Sandbox is Beta: a workspace admin enables it on the workspace
    Previews page. ``apx-agent doctor --online`` checks it is on.

    Args:
        sandbox_id: A named sandbox to reuse. apx creates it on first use and
            starts it when it has stopped; files under ``/home/sandbox-agent``
            persist between calls. ``None`` (default) creates a sandbox for
            each call and deletes it afterwards.
        auth: ``"user"`` (default) runs as the calling user (OBO), so sandbox
            code can reach only what that user can. ``"app"`` runs as the App
            service principal, like ``agentbricks tools add sandbox --auth app``.
        name: Tool name shown to the LLM.
        description: Tool description shown to the LLM.
        wait_s: Longest total wait (sandbox start plus the command). Past it the
            command is terminated and the tool raises a ``ToolError``.

    Example::

        from apx_agent import Agent, sandbox_tool

        agent = Agent(tools=[sandbox_tool()])
    """
    if auth not in ("user", "app"):
        raise ValueError("sandbox_tool() auth must be 'user' or 'app'")
    if wait_s <= 0:
        raise ValueError("sandbox_tool() requires a positive wait_s")

    from ._defaults import ClientDependency, UserClientDependency
    from ._resources import ServiceBinding, bind_service
    from ._tool_factory import build_tool

    _desc = description or (
        "Run Python or shell code in an isolated Databricks Sandbox (Linux, 4 vCPUs, 8 GB RAM) "
        "and return its exit code, stdout and stderr. Use it to compute, transform files, or "
        "test code. language is 'python' (python3 -c) or 'shell' (bash -c). Print what you need "
        f"to see. Files under /home/sandbox-agent{' persist between calls' if sandbox_id else ' last one call'}."
    )

    async def _execute(code: str, language: str, ws: Any) -> dict[str, Any]:
        if language not in _COMMANDS:
            raise ToolError(f"language must be 'python' or 'shell', got {language!r}")
        deadline = time.monotonic() + wait_s
        try:
            # The worker stops at ``deadline`` (start polling, then the command's
            # execution_timeout); wait_for only backstops a hung HTTP call.
            response = await asyncio.wait_for(
                asyncio.to_thread(_run, ws, sandbox_id, language, code, deadline),
                timeout=wait_s + _BACKSTOP_S,
            )
        except (asyncio.TimeoutError, TimeoutError) as e:  # distinct classes before Python 3.11
            raise ToolError(f"Sandbox did not finish within {wait_s:.0f}s.") from e
        except Exception as e:
            raise _tool_error(e) from e
        return _result(response, wait_s)

    if auth == "user":
        async def _run_in_sandbox(
            code: str, ws: UserClientDependency, language: Literal["python", "shell"] = "python",  # type: ignore[valid-type]
        ) -> dict[str, Any]:
            """Placeholder doc — overwritten below."""
            return await _execute(code, language, ws)
    else:
        async def _run_in_sandbox(  # type: ignore[misc]
            code: str, ws: ClientDependency, language: Literal["python", "shell"] = "python",  # type: ignore[valid-type]
        ) -> dict[str, Any]:
            """Placeholder doc — overwritten below."""
            return await _execute(code, language, ws)

    tool = build_tool(_run_in_sandbox, name=name, description=_desc)
    return bind_service(tool, ServiceBinding(SANDBOX_SERVICE, auth))
