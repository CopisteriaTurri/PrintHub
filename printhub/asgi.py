"""Punto d'ingresso ASGI: HTTP per l'API e l'admin, WebSocket per gli agenti."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "printhub.settings")

# Django va inizializzato prima di importare codice che usa i modelli.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from django.urls import path  # noqa: E402

from hub.consumers import AgentConsumer  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": URLRouter([path("agent/v1", AgentConsumer.as_asgi())]),
    }
)
