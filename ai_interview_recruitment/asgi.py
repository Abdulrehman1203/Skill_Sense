"""
ASGI config for ai_interview_recruitment project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_interview_recruitment.settings')

django_application = get_asgi_application()

# Initialize Django before importing model-backed consumers. No Django session
# AuthMiddlewareStack: Clerk-issued opaque tickets are the sole socket credential.
from channels.routing import ProtocolTypeRouter, URLRouter
from interview_system.routing import websocket_urlpatterns
from interview_system.socket_security import SecureSocketEntry

application = ProtocolTypeRouter({
    'http': django_application,
    'websocket': SecureSocketEntry(URLRouter(websocket_urlpatterns)),
})
