"""Re-exports for the resumes package."""

from .extraction import ExtractionError, extract_text

__all__ = ("extract_text", "ExtractionError")
