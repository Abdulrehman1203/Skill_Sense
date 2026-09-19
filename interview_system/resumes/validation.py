"""Validation shared by application and candidate-managed resume uploads."""

from __future__ import annotations

from typing import Any
from zipfile import BadZipFile, ZipFile

from rest_framework import serializers

from ..models import Resume


def validate_resume_upload(value: Any) -> Any:
    """Accept only non-empty PDF or DOCX files no larger than 5 MB."""
    filename = value.name.lower()
    if not filename.endswith((".pdf", ".docx")):
        raise serializers.ValidationError(
            "Unsupported file type. Only PDF and DOCX resumes are supported."
        )

    if value.size == 0:
        raise serializers.ValidationError("Resume file cannot be empty.")
    if value.size > Resume.MAX_FILE_SIZE_MB * 1024 * 1024:
        raise serializers.ValidationError(
            f"File size must not exceed {Resume.MAX_FILE_SIZE_MB}MB."
        )

    position = value.tell()
    try:
        value.seek(0)
        header = value.read(8)
        if filename.endswith(".pdf"):
            if not header.startswith(b"%PDF-"):
                raise serializers.ValidationError("File content is not a PDF.")
        else:
            if not header.startswith(b"PK\x03\x04"):
                raise serializers.ValidationError("File content is not a DOCX archive.")
            value.seek(0)
            try:
                with ZipFile(value) as archive:
                    members = set(archive.namelist())
                    if not {"[Content_Types].xml", "word/document.xml"} <= members:
                        raise serializers.ValidationError("File is not a DOCX document.")
            except (BadZipFile, OSError, ValueError) as exc:
                raise serializers.ValidationError("File is not a valid DOCX archive.") from exc
    finally:
        value.seek(position)

    return value
