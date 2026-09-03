"""
Custom DRF permission classes for role-based access control and object ownership.
"""

from __future__ import annotations

from typing import Any

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from .models import User


class IsAdmin(BasePermission):
    """Allows access only to authenticated users with ADMIN role."""

    def has_permission(self, request: Request, view: Any) -> bool:
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) == User.Role.ADMIN
        )


class IsRecruiter(BasePermission):
    """Allows access only to authenticated users with RECRUITER role."""

    def has_permission(self, request: Request, view: Any) -> bool:
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) == User.Role.RECRUITER
        )


class IsCandidate(BasePermission):
    """Allows access only to authenticated users with CANDIDATE role."""

    def has_permission(self, request: Request, view: Any) -> bool:
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) == User.Role.CANDIDATE
        )


class IsOwner(BasePermission):
    """
    Object-level permission checking if the requested object belongs to the request.user.
    Supports objects that are User instances or models with a `user` foreign key.
    """

    def has_object_permission(self, request: Request, view: Any, obj: Any) -> bool:
        if not (request.user and request.user.is_authenticated):
            return False

        if isinstance(obj, User):
            return obj == request.user

        if hasattr(obj, "user"):
            return obj.user == request.user

        return False


class IsJobOwner(BasePermission):
    """
    Object-level permission for Job and JobSkill resources.

    Grants write access only to the recruiter who owns the job posting.
    Works for both Job instances (obj.recruiter.user) and JobSkill
    instances (obj.job.recruiter.user).
    """

    def has_object_permission(self, request: Request, view: Any, obj: Any) -> bool:
        if not (request.user and request.user.is_authenticated):
            return False

        # Direct Job object
        if hasattr(obj, "recruiter"):
            return obj.recruiter.user == request.user

        # JobSkill — traverse through parent job
        if hasattr(obj, "job") and hasattr(obj.job, "recruiter"):
            return obj.job.recruiter.user == request.user

        return False


class IsApplicationAccessible(BasePermission):
    """
    Object-level permission for Application resources.

    Grants access if the requesting user is:
      - The candidate who owns the application, OR
      - The recruiter who owns the job the application is for.

    Returns False (which DRF maps to 404 when using filter-based queryset
    scoping, or 403 when using get_object()) for everyone else.
    """

    def has_object_permission(self, request: Request, view: Any, obj: Any) -> bool:
        if not (request.user and request.user.is_authenticated):
            return False

        user = request.user

        # Candidate who owns the application
        if getattr(user, "role", None) == User.Role.CANDIDATE:
            candidate_profile = getattr(user, "candidate_profile", None)
            return candidate_profile is not None and obj.candidate_id == candidate_profile.pk

        # Recruiter who owns the job
        if getattr(user, "role", None) == User.Role.RECRUITER:
            recruiter_profile = getattr(user, "recruiter_profile", None)
            return (
                recruiter_profile is not None
                and obj.job.recruiter_id == recruiter_profile.pk
            )

        return False


class IsResumeAccessible(BasePermission):
    """
    Object-level permission for Resume resources.

    Grants access if the requesting user is:
      - The candidate who owns the resume, OR
      - A recruiter who owns a job that has an application linked to
        this resume.
    """

    def has_object_permission(self, request: Request, view: Any, obj: Any) -> bool:
        if not (request.user and request.user.is_authenticated):
            return False

        user = request.user

        # Candidate who owns the resume
        if getattr(user, "role", None) == User.Role.CANDIDATE:
            candidate_profile = getattr(user, "candidate_profile", None)
            return (
                candidate_profile is not None
                and obj.candidate_id == candidate_profile.pk
            )

        # Recruiter who owns a job linked via Application → Resume
        if getattr(user, "role", None) == User.Role.RECRUITER:
            from .models import Application

            recruiter_profile = getattr(user, "recruiter_profile", None)
            if recruiter_profile is None:
                return False
            return Application.objects.filter(
                resume=obj,
                job__recruiter=recruiter_profile,
            ).exists()

        return False

