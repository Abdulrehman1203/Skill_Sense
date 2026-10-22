"""
URL patterns for the interview_system app.

Jobs and skills are registered via DRF routers. Manual paths
(webhooks, /users/me/) coexist alongside the router-generated URLs.
"""

from django.urls import include, path
from .conversation_views import ConversationControlView
from rest_framework.routers import DefaultRouter

from .views import (
    ApplicationViewSet,
    AuditLogView,
    AdminAnalyticsView,
    IntegrityThresholdsView,
    CandidateProfileView,
    CandidateScoreView,
    CandidateResumeViewSet,
    JobSkillViewSet,
    JobViewSet,
    JobRankingView,
    InterviewViewSet,
    LoginRoleView,
    MeView,
    NotificationListView,
    NotificationUnreadCountView,
    NotificationMarkReadView,
    RecruiterProfileView,
    ResumeDetailView,
    ScoringRubricViewSet,
)
from .webhook_views import ClerkWebhookView
from .retell_webhook import RetellCallEndedView
from .socket_ticket_views import SocketTicketView
from .video_views import FinishVideoView
from .question_views import ApplicationQuestionSetView, QuestionSetView
from .evaluation_views import EvaluationHistoryView, EvaluationReleaseView, EvaluationFeedbackView, TranscriptHistoryView
from .scoring_views import ApplicationScoringView
from .feedback_views import CandidateFeedbackListView, CandidateFeedbackDetailView, FeedbackReviewView, FeedbackReleaseView
from .workflow_views import (EvaluationRerunView, TranscriptSelectionView, InterviewRescheduleView,
    InterviewReopenView, DecisionReversalView, FeedbackEditView, FeedbackRetractionView, WorkflowHistoryView)

app_name = "interview_system"

# ── DRF Router ───────────────────────────────────────────────

router = DefaultRouter()
router.register(r"jobs", JobViewSet, basename="job")
router.register(r"applications", ApplicationViewSet, basename="application")
router.register(r"interviews", InterviewViewSet, basename="interview")
router.register(r"candidates/resumes", CandidateResumeViewSet, basename="candidate-resume")
router.register(r"admin/rubrics", ScoringRubricViewSet, basename="admin-rubric")

# ── Nested skill routes under a specific job ─────────────────

job_skill_list = JobSkillViewSet.as_view({"get": "list", "post": "create"})
job_skill_detail = JobSkillViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

from .analysis_views import AnalysisStatusView, BehavioralReportView, AnalysisSnapshotView

urlpatterns = [
    path('interviews/<uuid:pk>/rerun-evaluation/', EvaluationRerunView.as_view(), name='evaluation-rerun'),
    path('interviews/<uuid:pk>/select-transcript/', TranscriptSelectionView.as_view(), name='transcript-selection'),
    path('interviews/<uuid:pk>/reschedule/', InterviewRescheduleView.as_view(), name='interview-reschedule'),
    path('interviews/<uuid:pk>/reopen/', InterviewReopenView.as_view(), name='interview-reopen'),
    path('applications/<uuid:pk>/reverse-decision/', DecisionReversalView.as_view(), name='decision-reversal'),
    path('applications/<uuid:pk>/workflow-history/', WorkflowHistoryView.as_view(), name='workflow-history'),
    path('feedback/<uuid:pk>/edit/', FeedbackEditView.as_view(), name='feedback-edit'),
    path('feedback/<uuid:pk>/retract/', FeedbackRetractionView.as_view(), name='feedback-retract'),
    path('feedback/', CandidateFeedbackListView.as_view(), name='feedback-list'),
    path('feedback/<uuid:pk>/', CandidateFeedbackDetailView.as_view(), name='feedback-detail'),
    path('feedback/<uuid:pk>/release/', FeedbackReleaseView.as_view(), name='feedback-release'),
    path('interviews/<uuid:pk>/feedback-review/', FeedbackReviewView.as_view(), name='interview-feedback-review'),
    path("applications/<uuid:pk>/scoring/", ApplicationScoringView.as_view(), name="application-scoring"),
    path("interviews/<uuid:pk>/transcript-revisions/", TranscriptHistoryView.as_view(), name="interview-transcript-revisions"),
    path("interviews/<uuid:pk>/evaluations/", EvaluationHistoryView.as_view(), name="interview-evaluations"),
    path("interviews/<uuid:pk>/evaluation-feedback/", EvaluationFeedbackView.as_view(), name="interview-evaluation-feedback"),
    path("evaluations/<uuid:pk>/release/", EvaluationReleaseView.as_view(), name="evaluation-release"),
    path("applications/<uuid:pk>/question-set/", ApplicationQuestionSetView.as_view(), name="application-question-set"),
    path("question-sets/<uuid:pk>/", QuestionSetView.as_view(), name="question-set"),
    path("interview-sessions/<uuid:pk>/analysis-status/", AnalysisStatusView.as_view(), name="analysis-status"),
    path("interview-sessions/<uuid:pk>/behavioral-analysis/", BehavioralReportView.as_view(), name="behavioral-analysis"),
    path("interview-sessions/<uuid:pk>/analysis-snapshot/", AnalysisSnapshotView.as_view(), name="analysis-snapshot"),
    path("interview-sessions/<uuid:pk>/finish-video/", FinishVideoView.as_view(), name="finish-video"),
    path("interview-sessions/<uuid:pk>/ws-ticket/", SocketTicketView.as_view(), name="ws-ticket"),
    path("notifications/", NotificationListView.as_view(), name="notification-list"),
    path("notifications/unread-count/", NotificationUnreadCountView.as_view(), name="notification-unread-count"),
    path("notifications/<uuid:pk>/", NotificationMarkReadView.as_view(), name="notification-mark-read"),
    path("admin/audit-log/", AuditLogView.as_view(), name="admin-audit-log"),
    path("admin/analytics/", AdminAnalyticsView.as_view(), name="admin-analytics"),
    path("admin/integrity-thresholds/", IntegrityThresholdsView.as_view(), name="admin-integrity-thresholds"),
    path("webhooks/retell/call-ended/", RetellCallEndedView.as_view(), name="retell-call-ended"),
    path("webhooks/retell/interview-control/", ConversationControlView.as_view(), name="retell-interview-control"),
    # Manual endpoints
    path("auth/clerk/webhook/", ClerkWebhookView.as_view(), name="clerk-webhook"),
    path("auth/login-role/", LoginRoleView.as_view(), name="login-role"),
    path("users/me/", MeView.as_view(), name="me"),

    # Profile endpoints
    path("recruiters/profile/", RecruiterProfileView.as_view(), name="recruiter-profile"),
    path("candidates/profile/", CandidateProfileView.as_view(), name="candidate-profile"),
    path("candidates/<uuid:pk>/score/", CandidateScoreView.as_view(), name="candidate-score"),
    path("jobs/<uuid:pk>/ranking/", JobRankingView.as_view(), name="job-ranking"),

    # Nested job skills
    path("jobs/<uuid:job_pk>/skills/", job_skill_list, name="job-skill-list"),
    path("jobs/<uuid:job_pk>/skills/<int:pk>/", job_skill_detail, name="job-skill-detail"),

    # Resume detail (Phase 5)
    path("resumes/<uuid:pk>/", ResumeDetailView.as_view(), name="resume-detail"),

    # Router-generated job CRUD
    path("", include(router.urls)),
]
