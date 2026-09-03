"""
Gemini API client for structured resume parsing.

This is the **sole module** in the codebase that imports the Google GenAI SDK.
All prompt engineering, API key handling, timeouts, zero-temperature configuration,
and error wrapping live exclusively here.

Privacy boundary (hard requirement): Only plain resume text (``str``) is accepted and
transmitted to Gemini — never files, images, or raw binary payloads.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Maximum length of input text sent to the API
_MAX_TEXT_LENGTH = 15000


class GeminiParseError(Exception):
    """
    Unified exception for all Gemini parsing failure modes.

    Causes: empty input text, missing API key, API timeout / error,
    empty response (safety filter block), non-JSON output, or schema validation failure.
    This is the ONLY exception type that propagates out of this module.
    """


# ── Schema Validation ────────────────────────────────────────────


@dataclass
class EducationEntry:
    degree: str = ""
    institution: str = ""
    year: Any = None


@dataclass
class ExperienceEntry:
    title: str = ""
    company: str = ""
    duration: str = ""
    description: str = ""


@dataclass
class ParsedResumeSchema:
    """Expected shape of the Gemini JSON response."""

    skills: list[str] = field(default_factory=list)
    education: list[EducationEntry] = field(default_factory=list)
    experience: list[ExperienceEntry] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "skills": self.skills,
            "education": [
                {
                    "degree": e.degree,
                    "institution": e.institution,
                    "year": e.year,
                }
                for e in self.education
            ],
            "experience": [
                {
                    "title": ex.title,
                    "company": ex.company,
                    "duration": ex.duration,
                    "description": ex.description,
                }
                for ex in self.experience
            ],
            "certifications": self.certifications,
        }


def _validate_parsed_data(data: Any) -> dict[str, Any]:
    """
    Strictly validate top-level keys and container types, while being lenient on
    missing sub-fields within individual education/experience objects.

    Returns a clean dict or raises GeminiParseError.
    """
    if not isinstance(data, dict):
        raise GeminiParseError(
            f"Expected top-level JSON object, got {type(data).__name__}"
        )

    required_keys = {"skills", "education", "experience", "certifications"}
    missing_keys = required_keys - set(data.keys())
    if missing_keys:
        raise GeminiParseError(
            f"Response missing required top-level key(s): {sorted(list(missing_keys))}"
        )

    for key in required_keys:
        if not isinstance(data[key], list):
            raise GeminiParseError(
                f"Top-level key '{key}' must be a list, got {type(data[key]).__name__}"
            )

    # Validate & normalize skills
    clean_skills = [str(s) for s in data["skills"] if s is not None and str(s).strip()]

    # Validate & normalize education entries
    clean_education: list[EducationEntry] = []
    for entry in data["education"]:
        if not isinstance(entry, dict):
            raise GeminiParseError(
                f"Each entry in 'education' must be an object, got {type(entry).__name__}"
            )
        clean_education.append(
            EducationEntry(
                degree=str(entry.get("degree", "") or "").strip(),
                institution=str(entry.get("institution", "") or "").strip(),
                year=entry.get("year", None),
            )
        )

    # Validate & normalize experience entries
    clean_experience: list[ExperienceEntry] = []
    for entry in data["experience"]:
        if not isinstance(entry, dict):
            raise GeminiParseError(
                f"Each entry in 'experience' must be an object, got {type(entry).__name__}"
            )
        clean_experience.append(
            ExperienceEntry(
                title=str(entry.get("title", "") or "").strip(),
                company=str(entry.get("company", "") or "").strip(),
                duration=str(entry.get("duration", "") or "").strip(),
                description=str(entry.get("description", "") or "").strip(),
            )
        )

    # Validate & normalize certifications
    clean_certs = [
        str(c) for c in data["certifications"] if c is not None and str(c).strip()
    ]

    schema = ParsedResumeSchema(
        skills=clean_skills,
        education=clean_education,
        experience=clean_experience,
        certifications=clean_certs,
    )
    return schema.to_dict()


# ── Prompt ───────────────────────────────────────────────────────

_PARSE_PROMPT = """\
You are a resume parser. Extract structured data from the following resume text.

Return ONLY a JSON object with exactly these four keys:
- "skills": list of strings (technical and soft skills mentioned)
- "education": list of objects, each with keys "degree", "institution", "year"
- "experience": list of objects, each with keys "title", "company", "duration", "description"
- "certifications": list of strings

Rules:
- Return ONLY the JSON object, no markdown fences, no preamble, no commentary.
- If a section has no data, return an empty list [].
- "year" should be an integer or null if unknown.
- "duration" should be a human-readable string like "2 years" or "Jan 2020 - Mar 2022".

Resume text:
---
{resume_text}
---
"""


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` fences if the model wraps its response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


# ── Public API ───────────────────────────────────────────────────


def parse_resume_text(text: str) -> dict[str, Any]:
    """
    Send plain resume text to Gemini 2.5 Flash-Lite and return structured parsed data.

    Parameters
    ----------
    text : str
        Plain-text content of the resume. Must be non-empty.

    Returns
    -------
    dict
        Validated dictionary matching exact shape:
        {
          "skills": list[str],
          "education": list[dict],
          "experience": list[dict],
          "certifications": list[str]
        }

    Raises
    ------
    GeminiParseError
        The single exception type for all failure modes (empty text, missing key,
        timeout, API failure, empty response, invalid JSON, schema failure).
    """
    if not isinstance(text, str):
        raise GeminiParseError(f"Input must be a string, got {type(text).__name__}")

    cleaned_input = text.strip()
    if not cleaned_input:
        raise GeminiParseError("Cannot parse empty or whitespace-only resume text.")

    # Guard against absurdly long input
    truncated_text = cleaned_input[:_MAX_TEXT_LENGTH]

    # Resolve API Key lazily
    try:
        from django.conf import settings

        api_key = getattr(settings, "GEMINI_API_KEY", "")
    except Exception:
        import os

        api_key = os.getenv("GEMINI_API_KEY", "")

    if not api_key:
        raise GeminiParseError("GEMINI_API_KEY is not configured.")

    # Model choice: strictly gemini-2.5-flash-lite
    model_name = "gemini-2.5-flash-lite"

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        prompt = _PARSE_PROMPT.format(resume_text=truncated_text)

        config = types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        )

        response = client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=config,
        )
    except Exception as exc:
        raise GeminiParseError(f"Gemini API call failed: {exc}") from exc

    if not response or not getattr(response, "text", None):
        raise GeminiParseError("Gemini returned an empty response (possibly safety blocked).")

    raw_text = _strip_markdown_fences(response.text)

    try:
        parsed_json = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise GeminiParseError(
            f"Gemini response is not valid JSON: {exc}\nRaw text: {raw_text[:200]}"
        ) from exc

    return _validate_parsed_data(parsed_json)


# ── Internal Sanity Checks ───────────────────────────────────────

if __name__ == "__main__":
    print("Running gemini_client sanity checks...")

    # (a) Well-formed response parses correctly
    valid_data = {
        "skills": ["Python", "Django"],
        "education": [{"degree": "B.S. CS", "institution": "MIT", "year": 2020}],
        "experience": [
            {
                "title": "Backend Dev",
                "company": "Tech Corp",
                "duration": "2 years",
                "description": "Built REST APIs",
            }
        ],
        "certifications": ["AWS Certified Developer"],
    }
    result_a = _validate_parsed_data(valid_data)
    assert result_a["skills"] == ["Python", "Django"]
    assert len(result_a["education"]) == 1
    assert result_a["education"][0]["degree"] == "B.S. CS"
    print("  [OK] Check A: Well-formed response passed.")

    # (b) Missing required top-level key raises GeminiParseError
    missing_key_data = {
        "skills": ["Python"],
        "education": [],
        "experience": [],
        # missing "certifications"
    }
    try:
        _validate_parsed_data(missing_key_data)
        assert False, "Should have raised GeminiParseError for missing top-level key"
    except GeminiParseError as err:
        assert "certifications" in str(err)
        print("  [OK] Check B: Missing top-level key raised GeminiParseError.")

    # (c) Wrong field type raises GeminiParseError
    wrong_type_data = {
        "skills": "Python and Django",  # Should be list, not str
        "education": [],
        "experience": [],
        "certifications": [],
    }
    try:
        _validate_parsed_data(wrong_type_data)
        assert False, "Should have raised GeminiParseError for wrong container type"
    except GeminiParseError as err:
        assert "must be a list" in str(err)
        print("  [OK] Check C: Wrong container type raised GeminiParseError.")

    # (d) Missing sub-field fills sensible default
    missing_subfield_data = {
        "skills": ["Python"],
        "education": [{"institution": "Stanford"}],  # missing degree & year
        "experience": [{"company": "Startup Inc"}],  # missing title, duration, description
        "certifications": [],
    }
    result_d = _validate_parsed_data(missing_subfield_data)
    assert result_d["education"][0]["institution"] == "Stanford"
    assert result_d["education"][0]["degree"] == ""
    assert result_d["education"][0]["year"] is None
    assert result_d["experience"][0]["company"] == "Startup Inc"
    assert result_d["experience"][0]["title"] == ""
    print("  [OK] Check D: Missing sub-fields populated with defaults.")

    print("\nAll gemini_client sanity checks passed successfully!")
