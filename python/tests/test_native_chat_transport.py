"""Native chat mounts must retain the SDK invocation and identity boundary."""

import pytest

from tests.test_native_invocation_contract import native, request_body  # noqa: F401
from tests.test_runtime_targets import execution  # noqa: F401


def test_native_chat_mounts_without_legacy_handlers(native):
    assert native.client.get("/").status_code == 200
    assert "apxNativeChat" in native.client.get("/").text
    assert native.client.post("/invocations", json={}).status_code == 404
    assert native.client.post("/api/invocations", json=request_body()).status_code == 401
    # Apps do not enable the developer surface implicitly.
    assert native.client.get("/_apx/chat").status_code == 404


def test_native_chat_renderers_select_transport_explicitly():
    from apx_agent._ui_chat import _render_agent_ui
    from apx_agent._ui_root_chat import render_root_chat

    for render in [lambda **kw: _render_agent_ui(None, **kw), render_root_chat]:
        assert "apxNativeChat" not in render()
        assert "apxNativeChat" in render(native=True)


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_native_dev_chat_enablement(request, monkeypatch, enabled):
    monkeypatch.setenv("APX_DEV_UI", enabled)
    host = request.getfixturevalue("native")
    response = host.client.get("/_apx/chat")
    assert response.status_code == (200 if enabled == "1" else 404)
    if enabled == "1":
        assert "apxNativeChat" in response.text
        assert 'src="/_apx/vendor/' not in response.text
    assert host.client.post("/invocations", json={}).status_code == 404
    assert host.client.post("/_apx/eval/judge", json={}).status_code == 404
