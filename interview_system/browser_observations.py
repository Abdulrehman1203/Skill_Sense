"""Candidate-reported browser states, bounded and excluded from behavioral scoring."""
from datetime import datetime
from django.conf import settings
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from .socket_auth import SocketAuthorizationError, can_publish
from .socket_ticket_views import SocketTicketView
from .video_lifecycle import locked_session


def features():
    return {'visibility': settings.INTERVIEW_TAB_VISIBILITY_ENABLED,
            'screen_share': settings.INTERVIEW_SCREEN_SHARING_ENABLED}


def projection(session):
    enabled = features()
    output = {}
    for kind, active in enabled.items():
        if active:
            data = session.browser_observations.get(kind, {})
            output[kind] = {key: value for key, value in data.items() if key not in ('source', 'sequence')}
            output[kind].update(coverage='incomplete', label=(
                'Interview tab hidden' if kind == 'visibility' else 'Selected screen sharing surface'))
            output[kind]['coverage_ended_at'] = data.get('last_received_at')
            output[kind]['status_is_current'] = bool(data.get('last_received_at') and
                (timezone.now() - datetime.fromisoformat(data['last_received_at'])).total_seconds() <= 25
                and session.ended_at is None and session.audio_ended_at is None)
    return output


class BrowserObservationView(SocketTicketView):
    def get(self, request, pk):
        with locked_session(pk) as (_, session):
            if not can_publish(request.user, session):
                raise SocketAuthorizationError('access_denied')
            return Response({'features': features()})

    def post(self, request, pk):
        value = request.data
        states = {'visibility': ('visible', 'hidden', 'ended'),
                  'screen_share': ('sharing_monitor', 'sharing_window', 'sharing_browser', 'sharing_unknown',
                                   'stopped', 'denied_or_cancelled', 'unsupported', 'failed', 'ended')}
        if (not isinstance(value, dict) or set(value) != {'kind', 'state', 'sequence', 'source'}
                or not isinstance(value['kind'], str) or value['kind'] not in states
                or value['state'] not in states[value['kind']]
                or type(value['sequence']) is not int or not 0 <= value['sequence'] <= 1000000
                or not isinstance(value['source'], str) or len(value['source']) != 32
                or any(char not in '0123456789abcdef' for char in value['source'])):
            raise serializers.ValidationError('Invalid browser observation.')
        with locked_session(pk) as (interview, session):
            if not can_publish(request.user, session):
                raise SocketAuthorizationError('access_denied')
            if not features()[value['kind']]:
                return Response({'accepted': False})
            if (session.lifecycle_version != 1 or session.started_at is None or
                    session.audio_ended_at or session.ended_at or interview.status != 'LIVE'):
                return Response({'accepted': False})
            now = timezone.now()
            kind = value['kind']
            data = session.browser_observations.get(kind, {})
            if data.get('source') == value['source'] and value['sequence'] <= data.get('sequence', -1):
                return Response({'accepted': False})
            # Only a fresh source at sequence zero can replace an abandoned page.
            if data and data.get('source') != value['source'] and value['sequence'] != 0:
                return Response({'accepted': False})
            events = data.get('events', [])
            previous = data.get('last_received_at')
            gap = bool(previous and (now - datetime.fromisoformat(previous)).total_seconds() > 25)
            if gap:
                events = [*events, {'state': 'unknown', 'received_at': now.isoformat(),
                                   'previous_coverage_ended_at': previous}]
            changed = gap or data.get('state') != value['state'] or data.get('source') != value['source']
            if changed:
                events = [*events, {'state': value['state'], 'received_at': now.isoformat(),
                                   'previous_coverage_ended_at': data.get('last_received_at')}][-100:]
            session.browser_observations[kind] = {
                'source': value['source'], 'sequence': value['sequence'], 'state': value['state'],
                'last_received_at': now.isoformat(), 'events': events[-100:],
                'truncated': data.get('truncated', False) or (changed and len(data.get('events', [])) >= 100),
            }
            session.save(update_fields=['browser_observations'])
            return Response({'accepted': True})
