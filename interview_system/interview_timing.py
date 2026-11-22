"""Validation shared by scheduling commands and candidate admission."""
from datetime import timedelta

from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Interview, Job


def validated_timing(job_id, data, *, exclude=None):
    scheduled_at = data.get('scheduled_at')
    deadline_at = data.get('deadline_at')
    field = 'deadline_at' if deadline_at is not None else 'scheduled_at'
    instant = deadline_at if deadline_at is not None else scheduled_at
    if scheduled_at is not None:
        # Serialize competing bookings for a job, including reschedules.
        Job.objects.select_for_update().only('pk').get(pk=job_id)
    if instant is None or instant <= timezone.now():
        raise ValidationError({field: 'Choose a date and time in the future.'})
    if scheduled_at is not None:
        minute = scheduled_at.replace(second=0, microsecond=0)
        conflicts = Interview.objects.filter(
            application__job_id=job_id,
            status__in=(Interview.Status.SCHEDULED, Interview.Status.LIVE),
            scheduled_at__gte=minute, scheduled_at__lt=minute + timedelta(minutes=1),
        ).exclude(pk=exclude)
        if conflicts.exists():
            raise ValidationError({field: 'An interview is already booked at this time. Choose another time.'})
    return {'scheduled_at': scheduled_at, 'deadline_at': deadline_at}


def join_error(interview, *, enforce_fixed):
    if interview.status != Interview.Status.SCHEDULED:
        return None
    now = timezone.now()
    if interview.deadline_at is not None:
        if now >= interview.deadline_at:
            return {'deadline_at': 'The interview deadline has passed. Ask the recruiter for a new deadline.'}
    elif enforce_fixed and interview.scheduled_at and interview.scheduled_at > now + timedelta(minutes=15):
        return {'scheduled_at': 'You can join from 15 minutes before the scheduled time.'}
    return None
