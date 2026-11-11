"""Side-effect-free opt-in deployment probe; never contacts providers."""
import logging
import os
import socket
from celery import shared_task


@shared_task(bind=True, name='interview_system.tasks.deployment.ping')
def ping(self):
    evidence = {'status': 'pong', 'hostname': socket.gethostname(), 'pid': os.getpid(),
                'task_id': self.request.id}
    logging.getLogger(__name__).warning('DEPLOYMENT_PROBE executed task_id=%s hostname=%s pid=%s',
                                      evidence['task_id'], evidence['hostname'], evidence['pid'])
    return evidence
