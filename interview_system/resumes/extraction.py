"""
Resume text extraction module.

Supports PDF (via pdfplumber) and DOCX (via python-docx) with an OCR
fallback (pytesseract + pdf2image) for scanned / image-only PDFs.

This module is called exclusively from Celery tasks — never from the
request/response cycle.
"""

from __future__ import annotations

import io
import logging
import tempfile
from typing import TYPE_CHECKING, Any, IO, Union

if TYPE_CHECKING:
    from django.core.files import File

logger = logging.getLogger(__name__)

# Minimum character count to consider a PDF's text layer "real".
# Below this threshold we assume the PDF is scanned / image-only and
# fall back to OCR.
_MIN_TEXT_LENGTH = 50


class ExtractionError(Exception):
    """
    Raised when text cannot be extracted from a resume file.

    Causes: corrupt file, unsupported encoding, empty content after all
    extraction attempts (including OCR fallback).
    """


def _detect_mime(file_bytes: bytes) -> str:
    """Sniff the actual MIME type from raw bytes using python-magic."""
    import magic

    mime = magic.from_buffer(file_bytes, mime=True)
    return mime


def _extract_pdf_text(file_bytes: bytes) -> str:
    """
    Extract text from a PDF via pdfplumber.

    If the result is near-empty (image-only / scanned PDF), fall back
    to OCR via pdf2image + pytesseract (FR-14).
    """
    import pdfplumber

    text_parts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
    except Exception as exc:
        raise ExtractionError(f"Failed to parse PDF: {exc}") from exc

    full_text = "\n".join(text_parts).strip()

    if len(full_text) >= _MIN_TEXT_LENGTH:
        logger.info("PDF text extraction succeeded (%d chars)", len(full_text))
        return full_text

    # ── OCR fallback for scanned / image-only PDFs ────────────
    logger.info(
        "PDF text layer too short (%d chars), falling back to OCR",
        len(full_text),
    )
    return _ocr_pdf(file_bytes)


def _ocr_pdf(file_bytes: bytes) -> str:
    """Rasterize PDF pages and run pytesseract OCR on each."""
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError as exc:
        raise ExtractionError(
            "OCR dependencies (pdf2image, pytesseract) are not installed"
        ) from exc

    try:
        images = convert_from_bytes(file_bytes, dpi=300)
    except Exception as exc:
        raise ExtractionError(f"Failed to rasterize PDF for OCR: {exc}") from exc

    ocr_parts: list[str] = []
    for i, img in enumerate(images):
        try:
            raw_page_text = pytesseract.image_to_string(img)
            page_text = str(raw_page_text) if raw_page_text else ""
            ocr_parts.append(page_text)
        except Exception as exc:
            logger.warning("OCR failed on page %d: %s", i + 1, exc)

    full_text = "\n".join(ocr_parts).strip()
    if not full_text:
        raise ExtractionError(
            "OCR produced no text — the PDF may be corrupt or contain "
            "only non-text graphics."
        )

    logger.info("OCR fallback succeeded (%d chars)", len(full_text))
    return full_text


def _extract_docx_text(file_bytes: bytes) -> str:
    """Extract text from a DOCX file via python-docx."""
    try:
        from docx import Document
    except ImportError as exc:
        raise ExtractionError("python-docx is not installed") from exc

    try:
        doc = Document(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ExtractionError(f"Failed to parse DOCX: {exc}") from exc

    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    full_text = "\n".join(paragraphs).strip()

    if not full_text:
        raise ExtractionError("DOCX file contains no extractable text.")

    logger.info("DOCX text extraction succeeded (%d chars)", len(full_text))
    return full_text


def extract_text(resume_file: Union[IO[bytes], "File", Any]) -> str:
    """
    Extract plain text from a resume file (PDF or DOCX).

    Parameters
    ----------
    resume_file
        A file-like object (Django FieldFile, InMemoryUploadedFile, or
        plain ``IO[bytes]``). The file cursor is reset before reading.

    Returns
    -------
    str
        The extracted plain text.

    Raises
    ------
    ExtractionError
        If the file is corrupt, unsupported, or yields no text.
    """
    # Read all bytes — works for both Django storage files and plain IO
    try:
        resume_file.seek(0)
        file_bytes = resume_file.read()
    except Exception as exc:
        raise ExtractionError(f"Could not read resume file: {exc}") from exc

    if not file_bytes:
        raise ExtractionError("Resume file is empty (0 bytes).")

    # Detect real content type
    mime = _detect_mime(file_bytes)
    logger.info("Detected MIME type: %s", mime)

    if mime == "application/pdf":
        return _extract_pdf_text(file_bytes)

    # DOCX is a ZIP archive — python-magic reports it as a zip or
    # as the Office Open XML MIME type.
    docx_mimes = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/x-zip-compressed",
        "application/octet-stream",
    }
    if mime in docx_mimes:
        return _extract_docx_text(file_bytes)

    raise ExtractionError(
        f"Unsupported file type: {mime}. Only PDF and DOCX are accepted."
    )
