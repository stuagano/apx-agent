"""Field-description curation (#292 phase A): assemble per-column current-vs-
suggested state from an OKF bundle + UC comments, and write accepted
descriptions back into the bundle (read-after-write)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apx_agent._dev import build_dev_ui_router
from apx_agent._okf import okf_columns, write_okf_bundle
from apx_agent._ui_grounding import apply_column_descriptions, build_column_curation


class _FakeWs:
    """Minimal workspace client whose tables.list returns UC column comments."""

    def __init__(self, comments: dict):
        self._c = comments  # {table: {col: comment}}
        self.tables = SimpleNamespace(list=self._list)

    def _list(self, *, catalog_name: str, schema_name: str):
        return [
            SimpleNamespace(
                name=t, comment="",
                columns=[SimpleNamespace(name=c, comment=cm) for c, cm in cols.items()],
            )
            for t, cols in self._c.items()
        ]


def _bundle(tmp_path, *, descriptions=None):
    write_okf_bundle(
        {"catalog": "c", "schema": "s", "tables": {"orders": ["id(INT)", "amount(DOUBLE)"]}},
        tmp_path / "okf", timestamp="z", descriptions=descriptions or {},
    )
    return tmp_path / "okf"


# ── helpers ──────────────────────────────────────────────────────────────────


def test_build_curation_pairs_current_with_uc_suggestions(tmp_path):
    okf = _bundle(tmp_path, descriptions={"orders": {"id": "the order id"}})
    ws = _FakeWs({"orders": {"id": "the order id", "amount": "order total in USD"}})
    state = build_column_curation(okf, ws)

    assert state["catalog"] == "c" and state["schema"] == "s"
    cols = {c["column"]: c for c in state["tables"][0]["columns"]}
    # id: UC comment equals the current description → nothing to suggest.
    assert cols["id"]["current"] == "the order id" and cols["id"]["suggested"] == ""
    # amount: no current description, UC has one → offered as a suggestion.
    assert cols["amount"]["current"] == "" and cols["amount"]["suggested"] == "order total in USD"
    assert cols["amount"]["type"]  # type carried through


def test_build_curation_without_ws_has_no_suggestions(tmp_path):
    okf = _bundle(tmp_path, descriptions={"orders": {"id": "the order id"}})
    state = build_column_curation(okf, None)
    cols = {c["column"]: c for c in state["tables"][0]["columns"]}
    assert all(c["suggested"] == "" for c in cols.values())
    assert cols["id"]["current"] == "the order id"


def test_apply_writes_accepted_descriptions_into_bundle(tmp_path):
    okf = _bundle(tmp_path)
    n = apply_column_descriptions(okf, {"orders": {"amount": "order total"}})
    assert n == 1
    amount = next(c for c in okf_columns(okf)["orders"] if c["name"] == "amount")
    assert amount["description"] == "order total"  # read-after-write


# ── HTTP routes ──────────────────────────────────────────────────────────────


def _app(monkeypatch, okf_root, ws):
    monkeypatch.setattr("apx_agent._ui_grounding.resolve_okf_root", lambda start=None: okf_root)
    app = FastAPI()
    app.state.workspace_client = ws
    app.state.agent_context = None
    app.include_router(build_dev_ui_router())
    return app


@pytest.mark.asyncio
async def test_routes_round_trip_a_description(tmp_path, monkeypatch):
    okf = _bundle(tmp_path)
    ws = _FakeWs({"orders": {"amount": "order total in USD"}})
    app = _app(monkeypatch, okf, ws)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        got = (await ac.get("/_apx/grounding/columns")).json()
        amount = next(c for c in got["tables"][0]["columns"] if c["column"] == "amount")
        assert amount["suggested"] == "order total in USD"

        save = await ac.post(
            "/_apx/grounding/columns",
            json={"accepted": {"orders": {"amount": "order total in USD"}}},
        )
        assert save.status_code == 200 and save.json() == {"ok": True, "modified": 1}

        # Read-after-write through the route: now it's the current description.
        again = (await ac.get("/_apx/grounding/columns")).json()
        amount2 = next(c for c in again["tables"][0]["columns"] if c["column"] == "amount")
        assert amount2["current"] == "order total in USD" and amount2["suggested"] == ""


@pytest.mark.asyncio
async def test_no_bundle_get_is_empty_and_post_is_404(tmp_path, monkeypatch):
    app = _app(monkeypatch, None, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        got = await ac.get("/_apx/grounding/columns")
        assert got.status_code == 200 and got.json()["tables"] == []
        post = await ac.post("/_apx/grounding/columns", json={"accepted": {}})
        assert post.status_code == 404


@pytest.mark.asyncio
async def test_grounding_routes_published_to_openapi(tmp_path, monkeypatch):
    app = _app(monkeypatch, _bundle(tmp_path), None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        paths = (await ac.get("/openapi.json")).json()["paths"]
    assert "get" in paths["/_apx/grounding/columns"]
    assert "post" in paths["/_apx/grounding/columns"]


# ── Phase B: the curation panel ──────────────────────────────────────────────


def test_render_grounding_ui_ships_the_panel():
    from apx_agent._ui_grounding import render_grounding_ui

    html = render_grounding_ui()
    assert "<!DOCTYPE html>" in html
    # fetches the curation state and posts changes back to the same route
    assert "/_apx/grounding/columns" in html
    assert "async function load()" in html and "async function save()" in html
    assert "class=\"accept\"" in html  # per-field accept control
    assert "!d.ok" in html             # surfaces save failures (policy #4)
    assert 'href="/_apx/grounding"' in html  # canonical nav, active tab


@pytest.mark.asyncio
async def test_grounding_page_route_serves_html(tmp_path, monkeypatch):
    app = _app(monkeypatch, None, None)  # the page is static; renders without a bundle
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.get("/_apx/grounding")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Grounding" in r.text
