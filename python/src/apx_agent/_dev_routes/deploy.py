"""Dev UI deploy routes, split out of build_dev_ui_router (#1052)."""

from __future__ import annotations

from fastapi import APIRouter

from .context import DevRouteContext

from apx_agent._dev import (
    Any,
    Request,
    _find_deploy_root,
)


def register(router: APIRouter, ctx: DevRouteContext) -> None:
    """Attach this family's routes to the guarded dev router."""


    @router.get("/_apx/deploy/stream", include_in_schema=False)
    async def stream_deploy(request: Request) -> Any:
        import asyncio as _asyncio
        import re as _re
        import shutil
        from fastapi.responses import StreamingResponse

        root = _find_deploy_root()
        _ANSI = _re.compile(r"\x1b\[[0-9;]*m")

        async def _generate():
            if root is None:
                yield "data: ERROR: could not find project root (pyproject.toml)\n\n"
                yield "data: __EXIT__1\n\n"
                return
            apx_bin = shutil.which("apx")
            if apx_bin is None:
                yield "data: ERROR: apx binary not found in PATH\n\n"
                yield "data: __EXIT__1\n\n"
                return
            yield f"data: Running: apx-agent deploy {root}\n\n"
            try:
                proc = await _asyncio.create_subprocess_exec(
                    apx_bin, "deploy", str(root),
                    stdout=_asyncio.subprocess.PIPE,
                    stderr=_asyncio.subprocess.STDOUT,
                )
                assert proc.stdout is not None
                async for raw_line in proc.stdout:
                    line = _ANSI.sub("", raw_line.decode(errors="replace")).rstrip()
                    yield f"data: {line}\n\n"
                rc = await proc.wait()
                yield f"data: __EXIT__{rc}\n\n"
            except Exception as exc:
                yield f"data: __ERROR__{exc}\n\n"

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
