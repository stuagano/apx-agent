"""Claim-vs-reality: registration must retry on failure and not GC its task (#378).

_RegisterOnceMiddleware set `_registered = True` BEFORE scheduling `_register()`
and never stored the `asyncio.create_task(...)` handle. So (a) the event loop,
which keeps only a weak reference to bare tasks, could GC the attempt mid-flight
(it opens with `await asyncio.sleep(2)`), and (b) because the flag was already
True, a lost or failed attempt was never retried — the agent stayed absent from
the registry for the whole process lifetime.

This reconciles the *claim* ("register once, for real") against reality: a failed
attempt leaves `_registered` False and clears the task handle so the next request
retries; a successful attempt sets `_registered` True; and the in-flight task is
held by a strong reference (`_task`) while it runs.
"""

from __future__ import annotations

import asyncio
import httpx
from typing import Any

import pytest
from fastapi import FastAPI

from apx_agent._wiring import _schedule_registration


@pytest.mark.unit
def test_registration_retries_on_failure_and_sets_flag_on_success(monkeypatch) -> None:
    # No-op the 2s pre-registration sleep to keep the test fast; the test awaits
    # the real task handle (not a sleep) so scheduling stays deterministic.
    # _register does a local `import asyncio`, so patch the module directly.
    async def _nosleep(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _nosleep)

    outcome = {"fail": True}
    posts = {"n": 0}

    class _Resp:
        def raise_for_status(self) -> None:
            if outcome["fail"]:
                raise RuntimeError("registry 503")

        def json(self) -> dict:
            return {"id": "agent-1"}

    class _Client:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        async def post(self, *_a: Any, **_k: Any) -> _Resp:
            posts["n"] += 1
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    app = FastAPI()
    _schedule_registration(app, registry_url="http://reg", public_url="http://me")
    mw_cls = app.user_middleware[0].cls

    async def _drive() -> None:
        inst = mw_cls(app)

        async def _call_next(_req: Any) -> str:
            return "ok"

        # 1st request → attempt fails.
        await inst.dispatch(object(), _call_next)
        task = mw_cls._task
        assert task is not None, "attempt task not held by a strong reference"
        await task
        assert mw_cls._registered is False, "a failed attempt must not mark registered"
        assert mw_cls._task is None, "task ref not cleared → retry blocked"

        # 2nd request → RETRY, now succeeds.
        outcome["fail"] = False
        await inst.dispatch(object(), _call_next)
        task2 = mw_cls._task
        assert task2 is not None, "no retry after a failed attempt"
        await task2
        assert mw_cls._registered is True, "a successful attempt must mark registered"

    asyncio.run(_drive())
    assert posts["n"] == 2, f"expected a failed attempt + a retry, got {posts['n']} posts"
