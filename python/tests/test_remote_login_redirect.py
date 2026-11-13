"""A peer that redirects to sign-in fails with a clear error, not a JSON decode (#1178)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from apx_agent._remote import RemoteDatabricksAgent

_LOGIN = "https://ws.example.com/oidc/oauth2/v2.0/authorize?client_id=x"


@pytest.fixture
def redirecting(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.AsyncClient

    def routed(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": _LOGIN}))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", routed)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["_post_via_http", "_post_durable", "_stream_via_http"])
async def test_login_redirect_names_the_missing_credential(redirecting: None, call: str) -> None:
    remote = RemoteDatabricksAgent("https://peer.test")
    messages = [{"role": "user", "content": "hi"}]
    with pytest.raises(RuntimeError, match="redirected to sign-in") as err:
        if call == "_post_durable":
            await remote._post_durable(messages, {}, "https://peer.test/api/invocations")
        elif call == "_stream_via_http":
            async for _ in remote._stream_via_http(messages, {}):
                pass
        else:
            await getattr(remote, call)(messages, {})
    assert "302" in str(err.value)
    assert "client_id" not in str(err.value)
