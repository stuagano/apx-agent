from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Override volume path so the test doesn't write to UC.
    import config as cfg_mod
    cfg_mod.get_settings.cache_clear()  # clear lru_cache on the real function
    fake_settings = cfg_mod.load_settings(
        cfg_mod._DEFAULT_PATH
    ).model_copy(update={
        "volumes": cfg_mod.Volumes(raw=str(tmp_path), uploads=str(tmp_path)),
    })
    # Patch both the config module attribute AND the router's local binding so
    # the endpoint (which did `from .config import get_settings`) sees the fake.
    monkeypatch.setattr(cfg_mod, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(
        "api.get_settings",
        lambda: fake_settings,
    )
    yield TestClient(app)


def _make_pdf_bytes(text: str = "Synthetic contract for upload test.") -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 800, text)
    c.save()
    return buf.getvalue()


def test_upload_pdf_round_trip(client):
    pdf = _make_pdf_bytes()
    resp = client.post(
        "/api/upload-contract",
        files={"file": ("foo.pdf", pdf, "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["volume_path"].endswith(".pdf")
    assert body["size_bytes"] == len(pdf)


def test_upload_rejects_non_pdf(client):
    resp = client.post(
        "/api/upload-contract",
        files={"file": ("foo.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400
