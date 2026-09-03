"""
URL configuration for ai_interview_recruitment project.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

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

    # ── App API (currently empty — Phase 3 adds Clerk endpoints) ──
    path("api/", include("interview_system.urls", namespace="interview_system")),
]

# Serve media files in development (Django ignores this when DEBUG=False)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
