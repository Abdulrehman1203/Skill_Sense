"""Pure, deterministic scoring for the Phase 6 candidate rubric."""

from __future__ import annotations

import math
from numbers import Real
from typing import Any


_MISSING_REASONS = {
    "match": "match signal unavailable — resume parsing failed entirely",
    "interview": "interview signal unavailable — interview not yet conducted",
    "behavioral": "behavioral signal unavailable — vision pipeline did not run",
}


def _number(value: Any, name: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        bound = f"{minimum}–{maximum}" if maximum is not None else f"at least {minimum}"
        raise ValueError(f"{name} must be in the range {bound}")
    return result


def score(match_score: float | None, interview_signal: float | None,
          behavioral_signal: dict | None, rubric: Any) -> dict:
    """Return a 0–100 score from the available, weighted scoring signals.

    ``match_score`` is the Phase 5 value on 0–100; ``interview_signal`` is
    on 0–1. Rubric weights must each be on 0–1 and sum to one.

    Breakdown convention: every key is present; unavailable signals use
    ``None`` to distinguish them from a genuine zero score. Available values
    are *unweighted* 0–100 scores, rounded to two decimal places. A missing
    signal contributes exactly zero to the final sum, without redistributing
    its rubric weight.

    Behavioral conversion: clamp(attention_pct / 100 -
    0.10 * integrity_flag_count, 0, 1). Thus each integrity flag subtracts
    ten percentage points from the attention-derived score.
    """
    weights = {
        "match": _number(rubric.weight_match, "weight_match", 0.0, 1.0),
        "interview": _number(rubric.weight_interview, "weight_interview", 0.0, 1.0),
        "behavioral": _number(rubric.weight_behavioral, "weight_behavioral", 0.0, 1.0),
    }
    if not math.isclose(math.fsum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("rubric weights must sum to 1.0")

    normalized: dict[str, float | None] = {
        "match": None if match_score is None else _number(match_score, "match_score", 0.0, 100.0) / 100.0,
        "interview": None if interview_signal is None else _number(interview_signal, "interview_signal", 0.0, 1.0),
        "behavioral": None,
    }
    if behavioral_signal is not None:
        if not isinstance(behavioral_signal, dict):
            raise TypeError("behavioral_signal must be a dict or None")
        attention = _number(behavioral_signal["attention_pct"], "attention_pct", 0.0, 100.0)
        flags = _number(behavioral_signal["integrity_flag_count"], "integrity_flag_count", 0.0)
        if not flags.is_integer():
            raise ValueError("integrity_flag_count must be a whole number")
        normalized["behavioral"] = max(0.0, min(1.0, attention / 100.0 - 0.10 * flags))

    # Sum full-precision normalized contributions once, then round the
    # 0–100 final score. In particular, three missing signals yield 0.0.
    final_score = round(100.0 * math.fsum(
        weights[name] * (value if value is not None else 0.0)
        for name, value in normalized.items()
    ), 2)
    breakdown = {
        name: None if value is None else round(value * 100.0, 2)
        for name, value in normalized.items()
    }

    available = [
        f"{name} {breakdown[name]:.2f}/100 × {weights[name]:.4f}"
        for name, value in normalized.items() if value is not None
    ]
    explanation = (
        "Weighted components: " + "; ".join(available) + "."
        if available else "No scoring signals available; final score is 0.00."
    )
    unavailable = [_MISSING_REASONS[name] for name, value in normalized.items() if value is None]
    if unavailable:
        explanation += " " + "; ".join(unavailable) + "."

    return {"final_score": final_score, "breakdown": breakdown, "explanation": explanation}
