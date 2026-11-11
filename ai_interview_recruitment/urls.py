"""
URL configuration for ai_interview_recruitment project.
"""

from django.conf import settings
from pathlib import Path
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from interview_system.health import live, ready
from interview_system.metrics import metrics
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

urlpatterns = [
    path("api/health/", live, name="health"),
    path("api/health/live/", live, name="health-live"),
    path("api/health/ready/", ready, name="health-ready"),
    path("api/metrics/", metrics, name="metrics"),
    path("admin/", admin.site.urls),

    # ── OpenAPI schema (raw JSON/YAML) ────────────────────────
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),

    # ── Swagger UI  →  http://localhost:8000/api/docs/ ────────
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),

    # ── ReDoc UI  →  http://localhost:8000/api/redoc/ ─────────
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),

    # ── App API (currently empty — Phase 3 adds Clerk endpoints) ──
    path("api/", include("interview_system.urls", namespace="interview_system")),
]

# Only public company logos in local development. Never expose the resume tree.
if settings.DEBUG:
    urlpatterns += static('/media/recruiter/logos/', document_root=Path(settings.MEDIA_ROOT) / 'recruiter' / 'logos')
