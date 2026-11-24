"""Download recruiter workspace records and remove a Clerk account."""

from urllib.parse import quote

import requests
from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Application, ClerkWebhookState, Interview, Job, JobSkill, RecruiterCalendarConnection,
    RecruiterProfile, SocketTicket, User,
)
from .permissions import IsRecruiter
from .serializers import RecruiterProfileSerializer


class WorkspaceExportView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    def get(self, request):
        profile = get_object_or_404(RecruiterProfile, user=request.user)
        jobs = Job.objects.filter(recruiter=profile)
        applications = Application.objects.filter(job__recruiter=profile)
        interviews = Interview.objects.filter(application__job__recruiter=profile)
        response = JsonResponse({
            "version": 1,
            "exported_at": timezone.now(),
            "profile": RecruiterProfileSerializer(profile).data,
            "jobs": list(jobs.values(
                "id", "title", "description", "requirements", "skills_required",
                "location", "job_type", "experience_level", "salary", "status",
                "deadline", "screening_threshold", "screening_criteria",
                "auto_shortlist_enabled", "non_pass_policy", "created_at", "updated_at",
            )),
            "job_skills": list(JobSkill.objects.filter(job__recruiter=profile).values(
                "job_id", "skill_name", "is_required",
            )),
            "applications": list(applications.values(
                "id", "job_id", "candidate_id", "status", "created_at", "updated_at",
                "current_assessment__status", "current_assessment__match_score",
            )),
            "interviews": list(interviews.values(
                "id", "application_id", "status", "scheduled_at", "deadline_at",
                "completed_at", "completion_outcome", "created_at", "updated_at",
            )),
        })
        response["Content-Disposition"] = 'attachment; filename="skillsense-workspace.json"'
        response["Cache-Control"] = "private, no-store"
        return response


def deactivate_deleted_recruiter(clerk_id):
    """Scrub the recruiter identity while preserving linked hiring records."""
    recruiter = User.objects.filter(clerk_id=clerk_id, role=User.Role.RECRUITER)
    user_id = recruiter.values_list("pk", flat=True).first()
    if user_id is None:
        return
    now = timezone.now()
    recruiter.update(
        email=f"deleted-{user_id}@accounts.invalid", first_name="", last_name="",
        profile_image_url="", is_active=False, updated_at=now,
    )
    RecruiterProfile.objects.filter(user_id=user_id).update(phone="", position="")
    Job.objects.filter(recruiter_id=user_id, status=Job.Status.ACTIVE).update(
        status=Job.Status.CLOSED, updated_at=now,
    )
    RecruiterCalendarConnection.objects.filter(recruiter_id=user_id).delete()
    SocketTicket.objects.filter(user_id=user_id).delete()


class RecruiterAccountDeleteView(APIView):
    permission_classes = [IsAuthenticated, IsRecruiter]

    def post(self, request):
        if not isinstance(request.data, dict) or request.data.get("confirmation") != "DELETE":
            raise ValidationError({"detail": "Enter DELETE to confirm account deletion."})

        secret = settings.CLERK_SECRET_KEY
        if not secret:
            return Response({"detail": "Account deletion is unavailable."}, status=503)

        clerk_id = request.user.clerk_id
        try:
            response = requests.delete(
                f"https://api.clerk.com/v1/users/{quote(clerk_id, safe='')}",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=10,
                allow_redirects=False,
            )
            data = response.json()
            if response.status_code == 404:
                deleted = any(
                    error.get("code") == "resource_not_found"
                    for error in data.get("errors", [])
                )
            else:
                deleted = (
                    response.status_code == 200 and data.get("id") == clerk_id
                    and data.get("deleted") is True
                )
        except (requests.RequestException, ValueError, AttributeError, TypeError):
            deleted = False
        if not deleted:
            return Response({"detail": "Clerk could not confirm account deletion. Try again."}, status=502)

        # The signed webhook may arrive before this request finishes. Both paths
        # use the same lock and idempotent cleanup.
        with transaction.atomic():
            state, _ = ClerkWebhookState.objects.select_for_update().get_or_create(
                clerk_id=clerk_id, defaults={"last_event_at": timezone.now()},
            )
            deactivate_deleted_recruiter(clerk_id)
            state.is_deleted = True
            state.save(update_fields=["is_deleted"])
        return Response(status=204)
