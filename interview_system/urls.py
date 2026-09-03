"""
URL patterns for the interview_system app.

Jobs and skills are registered via DRF routers. Manual paths
(webhooks, /users/me/) coexist alongside the router-generated URLs.
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ApplicationViewSet,
    CandidateProfileView,
    CandidateResumeViewSet,
    JobSkillViewSet,
    JobViewSet,
    MeView,
    RecruiterProfileView,
    ResumeDetailView,
)
from .webhook_views import ClerkWebhookView

app_name = "interview_system"

# ── DRF Router ───────────────────────────────────────────────

router = DefaultRouter()
router.register(r"jobs", JobViewSet, basename="job")
router.register(r"applications", ApplicationViewSet, basename="application")
router.register(r"candidates/resumes", CandidateResumeViewSet, basename="candidate-resume")

# ── Nested skill routes under a specific job ─────────────────

job_skill_list = JobSkillViewSet.as_view({"get": "list", "post": "create"})
job_skill_detail = JobSkillViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

urlpatterns = [
    # Manual endpoints
    path("auth/clerk/webhook/", ClerkWebhookView.as_view(), name="clerk-webhook"),
    path("users/me/", MeView.as_view(), name="me"),

    # Profile endpoints
    path("recruiters/profile/", RecruiterProfileView.as_view(), name="recruiter-profile"),
    path("candidates/profile/", CandidateProfileView.as_view(), name="candidate-profile"),

    # Nested job skills
    path("jobs/<uuid:job_pk>/skills/", job_skill_list, name="job-skill-list"),
    path("jobs/<uuid:job_pk>/skills/<int:pk>/", job_skill_detail, name="job-skill-detail"),

    # Resume detail (Phase 5)
    path("resumes/<uuid:pk>/", ResumeDetailView.as_view(), name="resume-detail"),

    # Router-generated job CRUD
    path("", include(router.urls)),
]

