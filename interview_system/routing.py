from django.urls import path
from .consumers import ObserverConsumer, PublisherConsumer

from .live_monitor import LiveMonitorSocket

websocket_urlpatterns = [
    path('ws/monitor/<uuid:session_id>/<str:role>/', LiveMonitorSocket.as_asgi()),
    path('ws/analysis/<uuid:session_id>/', PublisherConsumer.as_asgi()),
    path('ws/analysis/<uuid:session_id>/observe/', ObserverConsumer.as_asgi()),
]
