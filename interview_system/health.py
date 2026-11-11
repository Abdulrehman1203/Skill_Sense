"""Public fixed-shape probes: no URLs, credentials or exception text."""
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
@require_GET
def live(request):
    return JsonResponse({'status': 'ok'})


@csrf_exempt
@require_GET
def ready(request):
    try:
        if getattr(settings, 'CLERK_REQUIRE_SECURE_CONFIG', False):
            from ai_interview_recruitment.clerk_config import validate
            validate({name: getattr(settings, name, None) for name in ('CLERK_ISSUER','CLERK_JWKS_URL','CLERK_SECRET_KEY','CLERK_WEBHOOK_SECRET','CLERK_AUTHORIZED_PARTIES')})
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        if getattr(settings, 'HEALTH_CHECK_MIGRATIONS', False):
            from django.db.migrations.executor import MigrationExecutor
            executor = MigrationExecutor(connection)
            if executor.migration_plan(executor.loader.graph.leaf_nodes()):
                raise RuntimeError('Pending schema')
        from redis import Redis
        for url in getattr(settings, 'HEALTH_REDIS_URLS', (settings.CELERY_BROKER_URL,)):
            with Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) as client:
                client.ping()
        if getattr(settings, 'HEALTH_CHECK_VISION', False) and settings.INTERVIEW_ANALYSIS_VISION_ENABLED:
            from .vision_inference import worker_ready
            if not worker_ready():
                raise RuntimeError('Vision lease unavailable')
    except Exception:
        return JsonResponse({'status': 'not_ready'}, status=503)
    return JsonResponse({'status': 'ready'})
