"""
URL configuration for ai_interview_recruitment project.
"""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from interview_system.auth_views import MeView

urlpatterns = [
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

    # ── Auth + Registration API ───────────────────────────────
    path("api/auth/", include("interview_system.urls", namespace="interview_system")),

    # ── Protected user endpoint ───────────────────────────────
    path("api/users/me/", MeView.as_view(), name="me"),
]


