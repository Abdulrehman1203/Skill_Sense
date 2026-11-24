"""Validation shared by application and candidate-managed resume uploads."""

from __future__ import annotations

import hashlib
import re
from typing import Any
from unicodedata import normalize
from zipfile import BadZipFile, ZipFile

from rest_framework import serializers

from ..models import Application, ApplicationAssessment, Resume
from .extraction import ExtractionError, extract_resume_opening


RESUME_REPLACEMENT_ERROR = "A resume submitted with an application cannot be replaced."


def resume_has_submission(resume: Resume) -> bool:
    return (
        Application.objects.filter(resume=resume).exists()
        or ApplicationAssessment.objects.filter(resume=resume).exists()
    )


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
                    if len(archive.infolist()) > 2000 or sum(entry.file_size for entry in archive.infolist()) > 20 * 1024 * 1024:
                        raise serializers.ValidationError('DOCX expanded content exceeds the document limit.')
                    if any(entry.flag_bits & 1 or entry.filename.startswith(('/', '\\'))
                           or '..' in entry.filename.replace('\\', '/').split('/') for entry in archive.infolist()):
                        raise serializers.ValidationError('DOCX archive contains unsupported entries.')
                    if not {"[Content_Types].xml", "word/document.xml"} <= members:
                        raise serializers.ValidationError("File is not a DOCX document.")
            except (BadZipFile, OSError, ValueError) as exc:
                raise serializers.ValidationError("File is not a valid DOCX archive.") from exc
    finally:
        value.seek(position)

    return value


def _name_words(value: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", normalize("NFKC", value).casefold())


def validate_resume_identity(upload, user) -> str:
    """Check the profile name against the resume heading and return the file digest."""
    first_name = _name_words(user.first_name)
    last_name = _name_words(user.last_name)
    profile_name = first_name + last_name
    if not profile_name:
        raise serializers.ValidationError(
            {"detail": "Add your name to your profile before uploading a resume."}
        )

    upload.seek(0)
    try:
        file_bytes = upload.read()
    finally:
        upload.seek(0)

    try:
        opening = extract_resume_opening(file_bytes, is_pdf=upload.name.lower().endswith(".pdf"))
    except ExtractionError as exc:
        raise serializers.ValidationError(
            {"detail": "Could not read your name. Upload a readable PDF or DOCX with your profile name at the top."}
        ) from exc

    lines = [line.strip() for line in opening.splitlines() if line.strip()][:8]
    for line in lines:
        heading = re.sub(r"^(?:full\s+)?name\s*:\s*", "", line, flags=re.IGNORECASE)
        heading = re.split(r"\s+[–—-]\s+|[|•·]", heading, maxsplit=1)[0]
        words = _name_words(heading)
        # A resume may include a middle initial or name absent from the profile.
        has_middle_name = (
            first_name and last_name and len(words) == len(profile_name) + 1
            and words[:len(first_name)] == first_name and words[-len(last_name):] == last_name
        )
        if words == profile_name or has_middle_name:
            return hashlib.sha256(file_bytes).hexdigest()

    raise serializers.ValidationError(
        {"detail": "Cannot upload this resume. Use a resume with the same name as your profile."}
    )


def reject_duplicate_resume(candidate, upload_hash: str) -> None:
    """Check saved uploads before saving a new one, with the candidate row locked."""
    resumes = Resume.objects.filter(candidate=candidate)
    message = {"detail": "You already uploaded this resume. Choose a different file."}
    if resumes.filter(upload_hash=upload_hash).exists():
        raise serializers.ValidationError(message)

    for resume in resumes.filter(upload_hash="").only("file").iterator():
        if not resume.file:
            continue
        try:
            with resume.file.open("rb") as source:
                stored_hash = hashlib.file_digest(source, "sha256").hexdigest()
        except FileNotFoundError:
            continue
        if stored_hash == upload_hash:
            raise serializers.ValidationError(message)
        resumes.filter(pk=resume.pk, upload_hash="").update(upload_hash=stored_hash)
