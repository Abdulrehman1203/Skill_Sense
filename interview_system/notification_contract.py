"""Closed notification vocabulary and role-safe rendered text; no sensitive prose."""
EVENT_MESSAGES = {
    'application_received': ('Application received', 'A new application has been received. Sign in to review it.'),
    'interview_scheduled': ('Interview scheduled', 'Your interview has been scheduled. Sign in to view the details.'),
    'score_ready': ('Score ready', 'A combined recommendation is ready for recruiter review.'),
    'application_status_changed': ('Application updated', 'Your application status has changed. Sign in to view the update.'),
    'question_review_ready': ('Questions ready', 'Interview questions are ready for recruiter review.'),
    'evaluation_ready': ('Evaluation ready', 'An interview answer evaluation is ready for recruiter review.'),
    'evaluation_failed': ('Evaluation unavailable', 'An interview evaluation could not be completed. Sign in to review its status.'),
    'feedback_released': ('Interview feedback released', 'Your recruiter has released interview feedback. Sign in to read your report.'),
    'feedback_retracted': ('Interview feedback withdrawn', 'A previously released feedback report was withdrawn. Sign in for its current status.'),
}
CANDIDATE_EVENTS = {'interview_scheduled', 'application_status_changed', 'feedback_released', 'feedback_retracted'}
INAPP_MESSAGES = {
    'application_received': 'A new application has been received. Open the application to review it.',
    'interview_scheduled': 'Your interview has been scheduled. View your interview details.',
    'application_status_changed': 'Your application status has changed. View your application for the latest update.',
    'evaluation_failed': 'An interview evaluation could not be completed. Open the review to check its status.',
    'feedback_released': 'Your recruiter has released interview feedback. View your report.',
    'feedback_retracted': 'A previously released feedback report was withdrawn. View its current status.',
}


def inapp_message(event, stored_message=None):
    """Render in-app copy, including rows saved with the old email wording."""
    if event not in EVENT_MESSAGES:
        return stored_message or ''
    if stored_message is not None and stored_message != EVENT_MESSAGES[event][1]:
        return stored_message
    return INAPP_MESSAGES.get(event, EVENT_MESSAGES[event][1])


RESOURCE_TYPES = {'application', 'interview', 'question_set', 'evaluation', 'score_revision', 'feedback_report'}
EVENT_RESOURCES = {'application_received': 'application', 'application_status_changed': 'application',
    'interview_scheduled': 'interview', 'score_ready': 'score_revision', 'question_review_ready': 'question_set',
    'evaluation_ready': 'evaluation', 'evaluation_failed': 'evaluation', 'feedback_released': 'feedback_report', 'feedback_retracted': 'feedback_report'}
LINK_FIELDS = {f'{name}_id' for name in RESOURCE_TYPES}


def event_name(event):
    name = event.get('type') if isinstance(event, dict) else event
    if not isinstance(name, str) or name not in EVENT_MESSAGES:
        raise ValueError('Unknown notification event')
    return name
