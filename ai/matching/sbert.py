"""
SBERT-based resume ↔ job description matching module.

**Pure Python — zero Django imports.**
This module contains no imports from Django, DRF, or application settings.
It can be imported, used, and unit-tested in complete isolation.

Model Loading Strategy:
- Lazy, module-level singleton pattern via ``_get_model()``.
- The SentenceTransformer model is loaded on the first invocation of ``match()``
  (never at module import time).
- Explicit CPU device placement (``device="cpu"``) to preserve GPU memory for
  Phase 8 vision inference models (SDD §9).

Skill Matching Strategy:
- Accepts ``job_skills: list[str] | None = None`` representing required skills.
- Performs case-insensitive substring search of each skill string against ``resume_text``.
- Skills found in ``resume_text`` are assigned to ``matched_skills``; remaining skills
  are assigned to ``missing_skills``.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

logger = logging.getLogger(__name__)

# Named constant for model selection
DEFAULT_MODEL_NAME = "all-MiniLM-L6-v2"

# Module-level singleton state
_model = None
_loaded_model_name: str | None = None


def _get_model():
    """
    Return the SentenceTransformer model instance, loading it on first call.

    The model is retained as a module-level singleton so Celery worker
    processes reuse a single memory footprint across task executions.
    """
    global _model, _loaded_model_name

    target_name = os.environ.get("SBERT_MODEL_NAME", DEFAULT_MODEL_NAME)

    if _model is None or _loaded_model_name != target_name:
        try:
            import torch
            if torch.get_num_threads() > 2:
                torch.set_num_threads(2)
        except Exception:
            pass

        from sentence_transformers import SentenceTransformer

        logger.info("Loading SBERT model '%s' on CPU...", target_name)
        _model = SentenceTransformer(target_name, device="cpu")
        _loaded_model_name = target_name
        logger.info("SBERT model loaded successfully.")

    return _model


def _compute_cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    try:
        import numpy as np

        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        dot = float(np.dot(a, b))
        norm_a = float(np.linalg.norm(a))
        norm_b = float(np.linalg.norm(b))
    except ImportError:
        dot = sum(x * y for x, y in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(x * x for x in vec_a))
        norm_b = math.sqrt(sum(y * y for y in vec_b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    similarity = dot / (norm_a * norm_b)
    # Clamp value strictly to range [0.0, 1.0]
    return max(0.0, min(1.0, float(similarity)))


def match(
    resume_text: str,
    job_text: str,
    job_skills: list[str] | None = None,
) -> dict[str, Any]:
    """
    Compute SBERT cosine similarity between resume text and job text,
    and categorize required skills into matched vs. missing lists.

    Parameters
    ----------
    resume_text : str
        Plain text content of the candidate's resume.
    job_text : str
        Plain text job title and description.
    job_skills : list[str] | None, optional
        List of required skill strings from the job posting.

    Returns
    -------
    dict
        Exact return shape:
        {
          "similarity": float,        # Cosine similarity in range [0.0, 1.0]
          "matched_skills": list[str], # Skills present in resume text
          "missing_skills": list[str]  # Skills missing from resume text
        }
    """
    skills_input = job_skills or []

    # Handle empty / whitespace-only inputs gracefully
    if not resume_text or not resume_text.strip() or not job_text or not job_text.strip():
        logger.warning("Empty input text provided to match(). Returning zero similarity.")
        return {
            "similarity": 0.0,
            "matched_skills": [],
            "missing_skills": list(skills_input),
        }

    # Lazy-load model on CPU
    model = _get_model()

    # Compute embeddings
    raw_resume_emb = model.encode(resume_text)
    resume_emb = (
        raw_resume_emb.tolist()
        if hasattr(raw_resume_emb, "tolist")
        else list(raw_resume_emb)
    )

    raw_job_emb = model.encode(job_text)
    job_emb = (
        raw_job_emb.tolist() if hasattr(raw_job_emb, "tolist") else list(raw_job_emb)
    )

    # Cosine similarity
    similarity = _compute_cosine_similarity(resume_emb, job_emb)

    # Substring skill matching (case-insensitive)
    matched_skills: list[str] = []
    missing_skills: list[str] = []

    resume_lower = resume_text.lower()
    for skill in skills_input:
        if skill.strip() and skill.strip().lower() in resume_lower:
            matched_skills.append(skill)
        else:
            missing_skills.append(skill)

    result = {
        "similarity": round(similarity, 4),
        "matched_skills": matched_skills,
        "missing_skills": missing_skills,
    }

    logger.info(
        "SBERT match completed: similarity=%.4f, matched=%d, missing=%d",
        result["similarity"],
        len(matched_skills),
        len(missing_skills),
    )

    return result


# ── Internal Sanity Checks ───────────────────────────────────────

if __name__ == "__main__":
    print("Running sbert.py sanity checks...")

    # Mock model for fast execution without loading heavy weights during fast test
    class MockModel:
        def encode(self, text: str):
            if "python" in text.lower() or "software" in text.lower():
                return [1.0, 0.0, 0.0]
            elif "chef" in text.lower() or "cooking" in text.lower():
                return [0.0, 1.0, 0.0]
            return [0.7, 0.7, 0.0]

    _model = MockModel()
    _loaded_model_name = DEFAULT_MODEL_NAME

    # (a) Similar texts score high similarity
    res_a = match(
        resume_text="Senior Python Software Engineer with Django experience",
        job_text="Python Software Developer position",
        job_skills=["Python", "Django", "Docker"],
    )
    assert res_a["similarity"] > 0.9, f"Expected high similarity, got {res_a['similarity']}"
    print("  [OK] Check A: Similar texts scored high similarity.")

    # (b) Unrelated texts score low similarity
    res_b = match(
        resume_text="Head Chef specializing in Italian cuisine and pasta",
        job_text="Python Software Developer position",
        job_skills=["Python"],
    )
    assert res_b["similarity"] < 0.1, f"Expected low similarity, got {res_b['similarity']}"
    print("  [OK] Check B: Unrelated texts scored low similarity.")

    # (c) Known skill present in resume shows in matched_skills
    assert "Python" in res_a["matched_skills"]
    assert "Django" in res_a["matched_skills"]
    print("  [OK] Check C: Present skills found in matched_skills.")

    # (d) Required skill absent from resume shows in missing_skills
    assert "Docker" in res_a["missing_skills"]
    print("  [OK] Check D: Absent skills found in missing_skills.")

    print("\nAll sbert.py sanity checks passed successfully!")
