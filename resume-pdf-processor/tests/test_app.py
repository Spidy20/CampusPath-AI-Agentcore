"""Unit tests for the resume PDF extractor Lambda."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pypdf import PdfWriter

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import app  # noqa: E402


def _make_pdf_bytes(pages: list[str], *, encrypt: bool = False) -> bytes:
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        # pypdf blank pages have no font operators; inject text via page contents is fragile.
        # For unit tests we stub extract_text on PdfReader pages instead when needed.
        _ = page
        writer.add_outline_item(text[:20] or "page", len(writer.pages) - 1)
    if encrypt:
        writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_normalize_text_collapses_whitespace():
    assert app._normalize_text("Hello   world\n\n\nNext") == "Hello world\n\nNext"


def test_validate_event_rejects_wrong_bucket():
    with pytest.raises(app.ExtractionError) as exc:
        app._validate_event({"bucket": "other-bucket", "key": "resume-uploads/a.pdf"})
    assert exc.value.status_code == 403


def test_validate_event_rejects_wrong_prefix():
    with pytest.raises(app.ExtractionError) as exc:
        app._validate_event({"bucket": app.ALLOWED_BUCKET, "key": "other/a.pdf"})
    assert exc.value.status_code == 403


def test_extract_text_from_pdf_rejects_non_pdf():
    with pytest.raises(app.ExtractionError, match="not a valid PDF"):
        app.extract_text_from_pdf(b"not-a-pdf")


def test_extract_text_from_pdf_rejects_encrypted_pdf():
    pdf_bytes = _make_pdf_bytes(["Secret resume content here " * 5], encrypt=True)
    with pytest.raises(app.ExtractionError, match="Encrypted PDFs"):
        app.extract_text_from_pdf(pdf_bytes)


def test_extract_text_from_pdf_success_with_stubbed_pages(monkeypatch):
    pdf_bytes = _make_pdf_bytes(["ignored"])
    page_text = "Jane Doe\nSoftware Engineer Intern\n" + ("Built APIs with Python. " * 4)

    class FakePage:
        def extract_text(self):
            return page_text

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]

    monkeypatch.setattr(app, "PdfReader", lambda *args, **kwargs: FakeReader())
    text, page_count, warnings = app.extract_text_from_pdf(pdf_bytes)
    assert page_count == 1
    assert "Jane Doe" in text
    assert len(text) >= app.MIN_TEXT_CHARS
    assert warnings == []


def test_extract_text_truncates_long_content(monkeypatch):
    pdf_bytes = _make_pdf_bytes(["ignored"])
    long_text = "A" * (app.MAX_TEXT_CHARS + 500)

    class FakePage:
        def extract_text(self):
            return long_text

    class FakeReader:
        is_encrypted = False
        pages = [FakePage()]

    monkeypatch.setattr(app, "PdfReader", lambda *args, **kwargs: FakeReader())
    text, _page_count, warnings = app.extract_text_from_pdf(pdf_bytes)
    assert len(text) == app.MAX_TEXT_CHARS
    assert any("truncated" in warning.lower() for warning in warnings)


def test_lambda_handler_success(monkeypatch):
    resume = "CampusPath Candidate\nPython, DSA, AWS\n" + ("Project experience details. " * 6)
    monkeypatch.setattr(app, "_load_pdf_bytes", lambda bucket, key: b"%PDF-fake")
    monkeypatch.setattr(
        app,
        "extract_text_from_pdf",
        lambda pdf_bytes: (resume, 1, []),
    )

    result = app.lambda_handler(
        {
            "bucket": app.ALLOWED_BUCKET,
            "key": f"{app.ALLOWED_PREFIX}demo.pdf",
            "filename": "demo.pdf",
        },
        None,
    )
    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["ok"] is True
    assert body["filename"] == "demo.pdf"
    assert body["page_count"] == 1
    assert body["resume_text"] == resume


def test_lambda_handler_maps_extraction_errors(monkeypatch):
    monkeypatch.setattr(
        app,
        "_validate_event",
        MagicMock(side_effect=app.ExtractionError("bad pdf", status_code=400)),
    )
    result = app.lambda_handler({"bucket": "x", "key": "y"}, None)
    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert body["ok"] is False
    assert body["error"] == "bad pdf"


def test_load_pdf_bytes_rejects_oversized_file(monkeypatch):
    fake_s3 = MagicMock()
    fake_s3.head_object.return_value = {"ContentLength": app.MAX_FILE_BYTES + 1}
    monkeypatch.setattr(app, "s3", fake_s3)

    with pytest.raises(app.ExtractionError, match="size limit"):
        app._load_pdf_bytes(app.ALLOWED_BUCKET, f"{app.ALLOWED_PREFIX}big.pdf")
