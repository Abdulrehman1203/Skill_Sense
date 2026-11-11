"""Propagate only a validated correlation ID; never log task arguments/results."""
import time
from celery.signals import before_task_publish, task_prerun, task_postrun, task_failure
from ai_interview_recruitment.operations import correlation
from ai_interview_recruitment.safe_logging import emit_event, request_id


def component(task):
    name = getattr(task, 'name', '')
    return 'screening' if 'screening' in name else 'notification' if 'notification' in name else 'other'


def publish(headers=None, **kwargs):
    if headers is not None:
        headers['x_correlation_id'] = correlation(request_id.get())


def started(task=None, **kwargs):
    headers = task.request.headers or {}
    task.request._ops_token = request_id.set(correlation(headers.get('x_correlation_id')))
    task.request._ops_started = time.monotonic()


def failed(sender=None, task_id=None, **kwargs):
    emit_event('task_failed', component=component(sender), task_id=task_id, count=1)


def ended(task=None, task_id=None, **kwargs):
    try:
        emit_event('task_completed', component=component(task), task_id=task_id,
                   duration_ms=max(0., time.monotonic() - getattr(task.request, '_ops_started', time.monotonic())) * 1000)
    finally:
        token = getattr(task.request, '_ops_token', None)
        if token is not None:
            request_id.reset(token)


for signal, callback, uid in ((before_task_publish, publish, 'ops.publish'), (task_prerun, started, 'ops.start'),
                             (task_postrun, ended, 'ops.end'), (task_failure, failed, 'ops.failure')):
    signal.connect(callback, weak=False, dispatch_uid=uid)
