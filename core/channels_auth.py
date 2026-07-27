"""
JWT authentication for Django Channels WebSocket connections.

The REST API authenticates via djangorestframework-simplejwt (Bearer header),
but Channels' stock AuthMiddlewareStack only recognizes Django session
cookies. A mobile client holding just a JWT access token has no session, so
without this middleware every WebSocket connect would see scope['user'] as
AnonymousUser and get rejected by ChatConsumer/NotificationConsumer.

The token is passed as a query string parameter (?token=<access_token>)
since WebSocketChannel.connect() doesn't support custom headers uniformly
across platforms.
"""
import logging
from urllib.parse import parse_qs

from channels.auth import AuthMiddlewareStack
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser

logger = logging.getLogger(__name__)


@database_sync_to_async
def _get_user_from_token(token):
    from django.contrib.auth import get_user_model
    from rest_framework_simplejwt.tokens import AccessToken
    from rest_framework_simplejwt.exceptions import TokenError

    User = get_user_model()
    try:
        validated_token = AccessToken(token)
        user_id = validated_token['user_id']
        return User.objects.get(id=user_id)
    except (TokenError, KeyError, User.DoesNotExist) as e:
        logger.debug(f"WebSocket JWT auth failed: {e}")
        return AnonymousUser()


class JWTAuthMiddleware:
    """Authenticates a WebSocket connection via a JWT access token in the query string."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query_string = scope.get('query_string', b'').decode()
        token = parse_qs(query_string).get('token', [None])[0]

        if token:
            scope['user'] = await _get_user_from_token(token)

        return await self.app(scope, receive, send)


def JWTAuthMiddlewareStack(inner):
    """JWT auth on top of the stock session-based AuthMiddlewareStack (session still works as a fallback)."""
    return JWTAuthMiddleware(AuthMiddlewareStack(inner))
