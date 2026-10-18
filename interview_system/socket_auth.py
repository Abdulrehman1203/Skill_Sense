"""Interview Analysis credentials. No socket messages, inference, or lifecycle mutations here."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
import hashlib
import math
import re
import secrets

from channels.db import database_sync_to_async
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from .authentication import ClerkJWTAuthentication
from .models import Interview, InterviewSession, SocketTicket, User

PURPOSES = ('publisher', 'observer')
SESSION_RELATIONS = ('interview__application__candidate', 'interview__application__job__recruiter')


class SocketAuthorizationError(Exception):
    def __init__(self, reason, status=403):
        self.reason = reason
        self.status = status
        super().__init__(reason)  # Only a fixed enum; never a credential or client input.


def can_publish(user, session):
    return bool(user.is_active and user.role == User.Role.CANDIDATE
                and session.interview.application.candidate.user_id == user.pk)


def can_observe(user, session):
    return bool(user.is_active and user.role == User.Role.RECRUITER
                and session.interview.application.job.recruiter.user_id == user.pk)


def require_eligibility(user, session, purpose):
    if not user.is_active:
        raise SocketAuthorizationError('account_inactive')
    allowed = can_publish(user, session) if purpose == 'publisher' else can_observe(user, session)
    if purpose not in PURPOSES or not allowed:
        raise SocketAuthorizationError('access_denied')
    if not session.video_required:
        raise SocketAuthorizationError('video_not_enabled', 409)
    if purpose == 'publisher' and (
        session.interview.status != Interview.Status.LIVE or session.started_at is None
        or session.ended_at is not None or session.audio_ended_at is not None
        or session.creation_uncertain or session.lifecycle_version != 1
        or session.admission_sealed_at is not None or session.video_state in ('finalizing', 'ended')
    ):
        raise SocketAuthorizationError('session_not_publishable', 409)


def _session(session_id):
    try:
        return InterviewSession.objects.select_related(*SESSION_RELATIONS).get(pk=session_id)
    except InterviewSession.DoesNotExist:
        raise SocketAuthorizationError('access_denied') from None


def _credential_context(user, claims, now):
    # claims must come from ClerkJWTAuthentication, never from a socket payload.
    exp = claims.get('exp') if isinstance(claims, dict) else None
    if isinstance(exp, bool) or not isinstance(exp, (float, int)) or not math.isfinite(exp):
        raise SocketAuthorizationError('credential_invalid', 401)
    try:
        expires = datetime.fromtimestamp(exp, dt_timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise SocketAuthorizationError('credential_invalid', 401) from None
    sid = claims.get('sid', '')
    if (claims.get('sub') != user.clerk_id or not isinstance(sid, str) or len(sid) > 255
            or claims.get('sts') == 'pending'):
        raise SocketAuthorizationError('credential_invalid', 401)
    if expires <= now:
        raise SocketAuthorizationError('credential_expired', 401)
    return expires, sid


@sensitive_variables('verified_claims', 'secret')
def issue_ticket(user_id, session_id, purpose, verified_claims):
    if purpose not in PURPOSES:
        raise SocketAuthorizationError('invalid_request', 400)
    with transaction.atomic():
        # Serializes issuance across all sessions/purposes for this user, bounding outstanding secrets.
        try:
            user = User.objects.select_for_update().get(pk=user_id)
        except User.DoesNotExist:
            raise SocketAuthorizationError('access_denied') from None
        now = timezone.now()
        credential_expiry, sid = _credential_context(user, verified_claims, now)
        session = _session(session_id)
        require_eligibility(user, session, purpose)
        SocketTicket.objects.filter(user=user, expires_at__lte=now).delete()
        outstanding = SocketTicket.objects.filter(user=user, consumed_at__isnull=True, expires_at__gt=now)
        if outstanding.count() >= max(1, settings.INTERVIEW_ANALYSIS_MAX_OUTSTANDING_TICKETS):
            raise SocketAuthorizationError('ticket_limit', 429)
        secret = secrets.token_urlsafe(32)
        expires = min(credential_expiry, now + timedelta(seconds=max(1, settings.INTERVIEW_ANALYSIS_TICKET_TTL_SECONDS)))
        SocketTicket.objects.create(
            digest=hashlib.sha256(secret.encode('ascii')).hexdigest(), user=user, session=session,
            purpose=purpose, clerk_session_id=sid, clerk_subject=user.clerk_id,
            credential_expires_at=credential_expiry, expires_at=expires,
        )
    suffix = 'observe/' if purpose == 'observer' else ''
    return {'ticket': secret, 'expires_at': expires, 'socket_path': f'/ws/analysis/{session.pk}/{suffix}',
            'protocol_version': 1}


@dataclass(frozen=True, repr=False)
class SocketLease:
    user_id: object
    session_id: object
    purpose: str
    clerk_subject: str
    clerk_session_id: str
    expires_at: datetime


@sensitive_variables('secret')
def redeem_ticket(secret, session_id, purpose):
    """One conditional UPDATE wins across processes. Invalid scope cannot redeem it.

    A correctly scoped claim is spent even when the subsequent eligibility check fails.
    The browser supplies only the secret; identity is read exclusively from the DB.
    """
    if purpose not in PURPOSES or not isinstance(secret, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', secret):
        raise SocketAuthorizationError('ticket_invalid', 401)
    digest = hashlib.sha256(secret.encode('ascii')).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        claim = SocketTicket.objects.filter(
            digest=digest, session_id=session_id, purpose=purpose, consumed_at__isnull=True,
            expires_at__gt=now, credential_expires_at__gt=now,
        )
        if claim.update(consumed_at=now) != 1:
            raise SocketAuthorizationError('ticket_invalid', 401)
        ticket = SocketTicket.objects.get(pk=digest)
    # A concurrent claim can wait on a DB lock across the ticket deadline.
    now = timezone.now()
    if ticket.expires_at <= now or ticket.credential_expires_at <= now:
        raise SocketAuthorizationError('ticket_invalid', 401)
    lease = SocketLease(ticket.user_id, ticket.session_id, ticket.purpose, ticket.clerk_subject,
                        ticket.clerk_session_id, min(ticket.credential_expires_at,
                        now + timedelta(seconds=max(1, settings.INTERVIEW_ANALYSIS_SOCKET_LEASE_SECONDS))))
    check_lease(lease)
    return lease


def check_lease(lease):
    """Call before admission/delivery and from a wall-clock watchdog (B3). Fail closed."""
    if timezone.now() >= lease.expires_at:
        raise SocketAuthorizationError('lease_expired', 401)
    try:
        user = User.objects.get(pk=lease.user_id, clerk_id=lease.clerk_subject)
    except User.DoesNotExist:
        raise SocketAuthorizationError('access_denied') from None
    require_eligibility(user, _session(lease.session_id), lease.purpose)
    return lease


# Entire sync operations, including ORM and any JWKS/provisioning HTTP, stay off the event loop.
# B3 handshakes use redeem_ticket_async, not socket-supplied JWTs or provisioning messages.
authenticate_clerk_credentials_async = database_sync_to_async(ClerkJWTAuthentication().authenticate_credentials)
redeem_ticket_async = database_sync_to_async(redeem_ticket)
check_lease_async = database_sync_to_async(check_lease)
