import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'balla.settings')

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()

# Import after Django is initialized
from channels.routing import ProtocolTypeRouter, URLRouter
from core.channels_auth import JWTAuthMiddlewareStack
from core.routing import websocket_urlpatterns  # Changed: removed 'balla.' prefix

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': JWTAuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})