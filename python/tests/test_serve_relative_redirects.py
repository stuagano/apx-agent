"""Apps proxy gives the app a loopback Host; redirects must not escape to it."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apx_agent._serve import _RelativeRedirects


def test_trailing_slash_redirect_is_path_only() -> None:
    app = FastAPI()

    @app.get("/_apx/agent")
    def agent() -> int:
        return 1

    app.add_middleware(_RelativeRedirects)
    client = TestClient(app, base_url="http://localhost:8000")
    r = client.get("/_apx/agent/?x=1", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/_apx/agent?x=1"
    assert client.get("/_apx/agent/").json() == 1


def test_foreign_host_redirect_untouched() -> None:
    from starlette.responses import RedirectResponse

    app = FastAPI()

    @app.get("/out")
    def out() -> RedirectResponse:
        return RedirectResponse("https://login.example.com/auth")

    app.add_middleware(_RelativeRedirects)
    r = TestClient(app, base_url="http://localhost:8000").get("/out", follow_redirects=False)
    assert r.headers["location"] == "https://login.example.com/auth"



def test_served_factory_wraps_create_app(monkeypatch) -> None:
    from apx_agent import _serve

    monkeypatch.setattr(_serve, "create_app", lambda: "native")
    assert _serve.create_served_app().app == "native"
