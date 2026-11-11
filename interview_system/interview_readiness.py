"""Pre-call checks: never create a paid call with unavailable required services."""
from urllib.parse import urlsplit

import requests
from django.conf import settings

CONTROL_PROTOCOL = 'skillsense.interview-control.v1'


def require_scoring_policy():
    from .models import ScoringPolicyState
    from ai.scoring.rubric import combined_score
    state = ScoringPolicyState.objects.select_related('active_rubric').filter(pk=1).first()
    rubric = state.active_rubric if state else None
    if not rubric or not rubric.active or rubric.policy_version != 'COMBINED_V1':
        raise InterviewNotReady('scoring_policy_unavailable', 'The interview scoring policy is not configured. Ask the recruiter to activate a rubric.')
    try:
        combined_score(None, None, weight_match=rubric.weight_match,
            weight_interview=rubric.weight_interview, weight_behavioral=rubric.weight_behavioral)
    except (ValueError, ArithmeticError):
        raise InterviewNotReady('scoring_policy_unavailable', 'The active scoring policy is invalid.') from None


class InterviewNotReady(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require_interview_ready(*, video_required):
    if video_required:
        if not settings.INTERVIEW_ANALYSIS_VISION_ENABLED:
            raise InterviewNotReady('video_processing_disabled',
                'Interview video processing is disabled. Ask the server operator to enable it before starting.')
        from .vision_inference import worker_ready
        try:
            ready = worker_ready()
        except Exception:
            ready = False
        if not ready:
            raise InterviewNotReady('video_worker_unavailable',
                'The interview video service is not ready. Please retry after the server operator starts the vision worker.')
    require_scoring_policy()
    if not settings.RETELL_CONVERSATION_GUARD_ENABLED:
        return
    url = settings.RETELL_CONVERSATION_CONTROL_URL
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment
            or not parsed.path.endswith('/api/webhooks/retell/interview-control/')):
        raise InterviewNotReady('interview_control_unavailable',
            'The interviewer callback is not configured. Ask the server operator to restore it before starting.')
    try:
        with requests.get(url, timeout=(1, 2), allow_redirects=False,
                          headers={'Accept': 'application/json', 'ngrok-skip-browser-warning': 'true'}) as response:
            ready = response.status_code == 200 and response.json() == {'protocol': CONTROL_PROTOCOL}
    except (requests.RequestException, ValueError):
        ready = False
    if not ready:
        raise InterviewNotReady('interview_control_unavailable',
            'The interviewer cannot reach the interview server. Ask the server operator to restore the public callback, then retry.')
