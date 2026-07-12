"""
URL patterns for the interview_system app — registration + JWT authentication.
"""

from django.urls import path

from .auth_views import LoginView, LogoutView, MeView, RefreshView
from .views import CandidateRegisterView, RecruiterRegisterView

app_name = "interview_system"

urlpatterns = [
    # ── Registration ─────────────────────────────────────────
    path(
        "register/recruiter/",
        RecruiterRegisterView.as_view(),
        name="register-recruiter",
    ),
    path(
        "register/candidate/",
        CandidateRegisterView.as_view(),
        name="register-candidate",
    ),

    # ── JWT Auth ─────────────────────────────────────────────
    path(
        "login/",
        LoginView.as_view(),
        name="login",
    ),
    path(
        "refresh/",
        RefreshView.as_view(),
        name="refresh",
    ),
    path(
        "logout/",
        LogoutView.as_view(),
        name="logout",
    ),
]



