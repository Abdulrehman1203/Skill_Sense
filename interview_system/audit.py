"""Explicit audit events: callers supply the actor; metadata is allowlisted."""
from django.core.exceptions import PermissionDenied

from .models import AuditLogEntry, User


def admin_actor(principal):
    """Bridge Django admin's separate identity to the platform audit FK."""
    if isinstance(principal, User):
        actor = principal
    else:
        email = getattr(principal, "email", "").strip()
        actor = User.objects.filter(email__iexact=email, role=User.Role.ADMIN, is_active=True).first() if email else None
    if actor is None or actor.role != User.Role.ADMIN or not actor.is_active:
        raise PermissionDenied("Audited admin writes require an active platform ADMIN account with the same email.")
    return actor


def record_audit(*, actor, action, target, changed_fields):
    """actor=None is intentional for a caller operating outside a request."""
    return AuditLogEntry.objects.create(
        actor=actor, action=action, target_type=target._meta.object_name,
        target_id=str(target.pk), metadata={"changed_fields": sorted(changed_fields)},
    )
