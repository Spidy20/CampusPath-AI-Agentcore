"""AWS Lambda handler that extracts selectable text from resume PDFs in S3."""

from __future__ import annotations

import io
import json
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

s3 = boto3.client("s3")

ALLOWED_BUCKET = os.environ.get("RESUME_BUCKET_NAME", "sagemaker-tutorials-mlhub")
ALLOWED_PREFIX = os.environ.get("RESUME_KEY_PREFIX", "resume-uploads/")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "10"))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "20000"))
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(5 * 1024 * 1024)))
MIN_TEXT_CHARS = 50


class ExtractionError(Exception):
    """Raised for expected, caller-facing extraction failures."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _normalize_text(value: str) -> str:
    text = value.replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _validate_event(event: dict[str, Any]) -> tuple[str, str, str | None]:
    bucket = str(event.get("bucket") or "").strip()
    key = str(event.get("key") or "").strip()
    filename = event.get("filename")
    filename = str(filename).strip() if filename else None

    if not bucket or not key:
        raise ExtractionError("Both 'bucket' and 'key' are required.")
    if bucket != ALLOWED_BUCKET:
        raise ExtractionError("Bucket is not allowed for resume extraction.", status_code=403)
    if not key.startswith(ALLOWED_PREFIX):
        raise ExtractionError("Object key prefix is not allowed.", status_code=403)
    if ".." in key or key.startswith("/"):
        raise ExtractionError("Object key is invalid.", status_code=400)
    return bucket, key, filename


def _load_pdf_bytes(bucket: str, key: str) -> bytes:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            raise ExtractionError("Resume PDF was not found in S3.", status_code=404) from exc
        raise ExtractionError("Unable to access the resume PDF in S3.", status_code=502) from exc

    size = int(head.get("ContentLength") or 0)
    if size <= 0:
        raise ExtractionError("Resume PDF is empty.")
    if size > MAX_FILE_BYTES:
        raise ExtractionError(
            f"Resume PDF exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB size limit."
        )

    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()
    except ClientError as exc:
        raise ExtractionError("Unable to download the resume PDF from S3.", status_code=502) from exc


def extract_text_from_pdf(pdf_bytes: bytes) -> tuple[str, int, list[str]]:
    """Extract selectable text from a PDF. Returns (text, page_count, warnings)."""
    if not pdf_bytes.startswith(b"%PDF"):
        raise ExtractionError("Uploaded file is not a valid PDF.")

    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    except PdfReadError as exc:
        raise ExtractionError("The PDF could not be parsed. Upload a valid text-based PDF.") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:  # noqa: BLE001 - surface a clean API error
            raise ExtractionError(
                "Encrypted PDFs are not supported. Export an unlocked PDF and try again."
            ) from exc
        if not unlocked:
            raise ExtractionError(
                "Encrypted PDFs are not supported. Export an unlocked PDF and try again."
            )
        warnings.append("Opened a password-protected PDF with an empty password.")

    page_count = len(reader.pages)
    if page_count == 0:
        raise ExtractionError("The PDF contains no pages.")
    if page_count > MAX_PAGES:
        raise ExtractionError(f"Resume PDFs are limited to {MAX_PAGES} pages.")

    parts: list[str] = []
    try:
        for index, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            normalized = _normalize_text(page_text)
            if normalized:
                parts.append(normalized)
            else:
                warnings.append(f"Page {index} produced little or no selectable text.")
    except FileNotDecryptedError as exc:
        raise ExtractionError(
            "Encrypted PDFs are not supported. Export an unlocked PDF and try again."
        ) from exc
    except PdfReadError as exc:
        raise ExtractionError("The PDF could not be read. Upload a valid text-based PDF.") from exc

    text = _normalize_text("\n\n".join(parts))
    if len(text) < MIN_TEXT_CHARS:
        raise ExtractionError(
            "Could not extract enough selectable text. This extractor supports text-based PDFs, "
            "not scanned image-only resumes."
        )

    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rstrip()
        warnings.append(
            f"Extracted text was truncated to {MAX_TEXT_CHARS} characters for resume review."
        )

    return text, page_count, warnings


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    try:
        if not isinstance(event, dict):
            raise ExtractionError("Request body must be a JSON object.")

        # Support API Gateway-style envelopes for local testing convenience.
        if "body" in event and isinstance(event["body"], str):
            event = json.loads(event["body"] or "{}")

        bucket, key, filename = _validate_event(event)
        pdf_bytes = _load_pdf_bytes(bucket, key)
        resume_text, page_count, warnings = extract_text_from_pdf(pdf_bytes)

        return _response(
            200,
            {
                "ok": True,
                "filename": filename or key.rsplit("/", 1)[-1],
                "page_count": page_count,
                "character_count": len(resume_text),
                "resume_text": resume_text,
                "warnings": warnings,
            },
        )
    except ExtractionError as exc:
        return _response(exc.status_code, {"ok": False, "error": str(exc)})
    except json.JSONDecodeError:
        return _response(400, {"ok": False, "error": "Request body must be valid JSON."})
    except Exception:
        return _response(
            500,
            {"ok": False, "error": "Unexpected error while extracting resume text."},
        )
