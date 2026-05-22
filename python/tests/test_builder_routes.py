"""Tests for the /_apx/builder SPA static-asset routes."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apx_agent._builder_routes import build_builder_router


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Stage a minimal _builder_ui_dist with one HTML and one JS asset
    dist_root = tmp_path / "_builder_ui_dist"
    (dist_root / "assets").mkdir(parents=True)
    (dist_root / "index.html").write_text(
        "<!doctype html><html><body><div id='root'></div>"
        "<script src='/_apx/builder/assets/main.js'></script>"
        "</body></html>"
    )
    (dist_root / "assets" / "main.js").write_text("console.log('builder');")

    # Patch the dist-locator to point at our tmp tree
    monkeypatch.setattr(
        "apx_agent._builder_routes._dist_root",
        lambda: dist_root,
    )
    app = FastAPI()
    app.include_router(build_builder_router())
    return app


def test_index_served(app):
    client = TestClient(app)
    r = client.get("/_apx/builder/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "id='root'" in r.text


def test_asset_served(app):
    client = TestClient(app)
    r = client.get("/_apx/builder/assets/main.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "console.log" in r.text


def test_spa_fallback(app):
    """Deep links like /_apx/builder/some/canvas-route fall back to index.html."""
    client = TestClient(app)
    r = client.get("/_apx/builder/some/canvas-route")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "id='root'" in r.text
