"""
Celery tasks package for interview_system.

Re-exports all 8 pipeline task stubs with production signatures, retry policies,
and persistence logic.
"""

from .analysis import aggregate_behavioral, generate_candidate_score
from .interviewing import (
    generate_questions,
    process_retell_transcript,
    schedule_interview,
)
from .notifications import send_notification
from .parsing import compute_match_score, parse_resume
from .screening import process_assessment, recover_stalled_assessments
from .retell import process_retell_event, recover_retell
from .video import recover_video
from .vision import process_frame, warm_vision

__all__ = (
    "process_frame",
    "warm_vision",
    "recover_video",
    "parse_resume",
    "compute_match_score",
    "schedule_interview",
    "generate_questions",
    "process_retell_transcript",
    "aggregate_behavioral",
    "generate_candidate_score",
    "send_notification",
    "process_assessment",
    "recover_stalled_assessments",
)
