from django.urls import path
from .consumers import ObserverConsumer, PublisherConsumer

websocket_urlpatterns = [
    path('ws/analysis/<uuid:session_id>/', PublisherConsumer.as_asgi()),
    path('ws/analysis/<uuid:session_id>/observe/', ObserverConsumer.as_asgi()),
]
