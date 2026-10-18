"""Local operator reads. No credentials, identities, images or model initialization."""
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Count, Min, Sum
from django.utils import timezone
from redis import Redis

from .models import BehavioralAnalysis, FrameAnalysis, FrameJob, InterviewSession
from ai.vision.measurements import percentiles


def health():
    instances = {}
    result = {'database': False, 'migrations_current': False, 'redis': {},
              'vision_enabled': settings.INTERVIEW_ANALYSIS_VISION_ENABLED, 'vision_ready': False}
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        result['database'] = True
        executor = MigrationExecutor(connection)
        result['migrations_current'] = not executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception:
        pass  # Never print connection exception strings/URLs.
    for name, url in (('broker', settings.CELERY_BROKER_URL),
                      ('channels', settings.INTERVIEW_ANALYSIS_REDIS_URL),
                      ('payload', settings.INTERVIEW_ANALYSIS_PAYLOAD_REDIS_URL)):
        client = Redis.from_url(url, socket_timeout=.4, socket_connect_timeout=.4, decode_responses=True)
        try:
            item = {'reachable': bool(client.ping()), 'policy_verified': False}
            instances[name] = client.info('server').get('run_id')
            try:
                config = client.config_get('save', 'appendonly', 'maxmemory-policy', 'maxmemory')
                item.update({'policy_verified': True, 'eviction': config.get('maxmemory-policy'),
                    'maxmemory_bytes': int(config.get('maxmemory', 0))})
                if name == 'payload':
                    item['nonpersistent'] = config.get('save') == '' and config.get('appendonly') == 'no'
                    item['no_replicas'] = client.info('replication').get('connected_slaves') == 0
            except Exception:
                pass  # Restricted ACLs can deny CONFIG; unknown is not compliant.
            result['redis'][name] = item
        except Exception:
            result['redis'][name] = {'reachable': False, 'policy_verified': False}
        finally:
            client.close()
    if settings.INTERVIEW_ANALYSIS_VISION_ENABLED:
        try:
            from .vision_inference import worker_ready
            result['vision_ready'] = worker_ready()
        except Exception:
            pass
    # Readiness does not certify TLS, provider, accuracy, queue throughput or GPU.
    result['ready'] = (result['database'] and result['migrations_current']
        and all(r['reachable'] for r in result['redis'].values())
        and (not settings.INTERVIEW_ANALYSIS_VISION_ENABLED or result['vision_ready']))
    payload = result['redis']['payload']
    payload['isolated_instance'] = bool(instances.get('payload') and instances.get('broker')
        and instances.get('channels') and instances['payload'] not in (instances['broker'], instances['channels']))
    result['payload_policy_compliant'] = (payload.get('nonpersistent') is True
        and payload.get('no_replicas') is True and payload.get('eviction') == 'noeviction'
        and payload.get('maxmemory_bytes', 0) > 0 and payload['isolated_instance'])
    return result


def metrics():
    """Bound detailed work to 1000 sessions / 10000 jobs; disclose truncation."""
    now = timezone.now()
    ids = list(InterviewSession.objects.filter(lifecycle_version=1, video_required=True)
               .order_by('-started_at', '-pk').values_list('pk', flat=True)[:1001])
    sessions = InterviewSession.objects.filter(pk__in=ids[:1000])
    counters = sessions.aggregate(**{name: Sum(name+'_count') for name in
                                    ('received', 'admitted', 'dropped')})
    jobs = FrameJob.objects.filter(session_id__in=ids[:1000])
    states = {r['state']: r['count'] for r in jobs.values('state').annotate(count=Count('pk'))}
    oldest = jobs.filter(state__in=('pending', 'processing')).aggregate(value=Min('received_at'))['value']
    rows = list(jobs.order_by('-received_at', '-pk').values_list(
        'received_at', 'terminal_at', 'state', 'attempts', 'publication_attempts')[:10001])
    latency = [(terminal-received).total_seconds()*1000 for received, terminal, state, _, _ in rows[:10000]
               if state == 'processed' and terminal is not None]
    finalizations = list(sessions.filter(analysis_finalized_at__isnull=False,
        admission_sealed_at__isnull=False).values_list('admission_sealed_at', 'analysis_finalized_at'))
    return {'schema_version': 1, 'sampled_at': now.isoformat(),
        'session_limit': 1000, 'sessions_truncated': len(ids) > 1000,
        'job_sample_limit': 10000, 'jobs_truncated': len(rows) > 10000,
        'counters': {k: v or 0 for k, v in counters.items()}, 'job_states': states,
        'oldest_active_seconds': max(0, (now-oldest).total_seconds()) if oldest else None,
        'processed_end_to_end_ms': percentiles(latency),
        'finalization_delay_ms': percentiles([(end-start).total_seconds()*1000 for start, end in finalizations]),
        'sample_worker_retries': sum(max(0, r[3]-1) for r in rows[:10000]),
        'sample_publication_retries': sum(max(0, r[4]-1) for r in rows[:10000]),
        'processing_ms': None, 'general_queue_latency_ms': None, 'cleanup_failures': None,
        'unavailable_reason': 'No durable service-start/general-probe/cleanup counter exists; use instrumented acceptance runs.',
        'rates': None, 'rates_reason': 'Calculate deltas between snapshots with identical session scope; totals are not rates.'}


def retention_plan(days):
    """Inventory only. No inferred retention policy, deletes or history rewrites."""
    cutoff = timezone.now()-timedelta(days=days)
    sessions = InterviewSession.objects.filter(lifecycle_version=1, analysis_finalized_at__lt=cutoff)
    return {'dry_run': True, 'days': days, 'cutoff': cutoff.isoformat(),
        'terminal_sessions': sessions.count(),
        'derived_observations': FrameAnalysis.objects.filter(session__in=sessions).count(),
        'job_metadata': FrameJob.objects.filter(session__in=sessions).count(),
        'reports': BehavioralAnalysis.objects.filter(session__in=sessions).count(),
        'action': 'none; approved retention/legal-hold and audit policy required before a deletion implementation'}
