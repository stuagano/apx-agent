from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from contract_parsing_agent.backend.extraction import read_pdf_text


def _make_pdf(path: Path, text: str) -> None:
    c = canvas.Canvas(str(path))
    for i, line in enumerate(text.splitlines() or [""]):
        c.drawString(72, 800 - i * 14, line)
    c.save()


def test_read_pdf_text_returns_text(tmp_path: Path) -> None:
    p = tmp_path / "sample.pdf"
    _make_pdf(
        p,
        "INTERCONNECTION AGREEMENT\n"
        "Counterparty: Acme Energy Co\n"
        "Term: 10 years\n"
        "Effective Date: January 1, 2025\n"
        "Expiration Date: December 31, 2034\n"
        "Annual Value: $1,200,000\n"
        "Jurisdiction: California\n"
        "Contract Type: Interconnection\n",
    )
    text = read_pdf_text(p)
    assert "INTERCONNECTION AGREEMENT" in text
    assert "Acme Energy Co" in text


def test_read_pdf_text_rejects_too_short(tmp_path: Path) -> None:
    p = tmp_path / "tiny.pdf"
    _make_pdf(p, "x")
    with pytest.raises(ValueError, match="too little text"):
        read_pdf_text(p, min_chars=100)
