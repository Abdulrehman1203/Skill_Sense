"""Explicit dedicated worker subscription; run as a separate process on Linux."""
import os


def main():
    if os.name == 'nt':
        raise SystemExit('Production vision prefork requires Linux; use the offline CPU experiment on Windows.')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_interview_recruitment.settings')
    os.environ['INTERVIEW_ANALYSIS_VISION_PROCESS'] = '1'
    import django
    django.setup()
    from django.conf import settings
    from .celery import app
    from celery.signals import worker_ready, worker_process_shutdown
    from interview_system.vision_inference import close_runtime
    from interview_system.tasks.vision import warm_vision
    worker_process_shutdown.connect(close_runtime, weak=False)
    def bootstrap(**kwargs):
        if settings.INTERVIEW_ANALYSIS_VISION_ENABLED:
            warm_vision.apply_async(queue=settings.INTERVIEW_ANALYSIS_VISION_QUEUE,
                routing_key=settings.INTERVIEW_ANALYSIS_VISION_QUEUE, priority=0, expires=10, retry=False)
    worker_ready.connect(bootstrap, weak=False)
    # Redis-only disable_prefetch is available in pinned Celery 5.6.3.
    app.conf.worker_disable_prefetch = True
    app.conf.worker_prefetch_multiplier = 1
    app.conf.worker_enable_remote_control = False
    arguments = ['worker', '-Q', settings.INTERVIEW_ANALYSIS_VISION_QUEUE, '--concurrency=1',
                     '--pool=prefork', '--loglevel=WARNING', '--hostname=vision@%h',
                     '--without-gossip', '--without-mingle']
    recycle = int(os.environ.get('INTERVIEW_ANALYSIS_VISION_MAX_TASKS_PER_CHILD', '0'))
    if not 0 <= recycle <= 10000:
        raise SystemExit('INTERVIEW_ANALYSIS_VISION_MAX_TASKS_PER_CHILD must be in 0..10000.')
    if recycle:
        arguments.append('--max-tasks-per-child='+str(recycle))
    app.worker_main(arguments)


if __name__ == '__main__':
    main()
