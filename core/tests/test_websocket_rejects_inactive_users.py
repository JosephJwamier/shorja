"""A deactivated account cannot open a WebSocket with a token it still holds.

JWTAuthMiddleware looked the user up by id alone. delete-account deactivates
the row and an admin ban does the same, but an access token issued before
lives on for up to 30 minutes, so the chat and live sockets stayed open to
that account. REST already refused it (simplejwt checks is_active); the
socket now matches.

transaction=True: the lookup runs in database_sync_to_async's own thread and
connection, which cannot see rows inside an uncommitted test transaction.
"""
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import AnonymousUser
from rest_framework_simplejwt.tokens import AccessToken

from core.channels_auth import JWTAuthMiddleware
from core.models import User

pytestmark = pytest.mark.django_db(transaction=True)


def user_seen_by_socket(token):
    seen = {}

    async def inner(scope, receive, send):
        seen['user'] = scope['user']

    scope = {'type': 'websocket', 'query_string': f'token={token}'.encode(),
             'user': AnonymousUser()}
    async_to_sync(JWTAuthMiddleware(inner))(scope, None, None)
    return seen['user']


def test_an_active_user_is_recognised():
    user = User.objects.create_user(username='ws_active', password='pass12345')

    seen = user_seen_by_socket(str(AccessToken.for_user(user)))

    assert seen.is_authenticated
    assert seen.pk == user.pk


def test_a_deactivated_user_is_anonymous_even_with_a_valid_token():
    user = User.objects.create_user(username='ws_gone', password='pass12345')
    token = str(AccessToken.for_user(user))
    user.is_active = False
    user.save(update_fields=['is_active'])

    seen = user_seen_by_socket(token)

    assert not seen.is_authenticated


def test_a_garbage_token_is_anonymous():
    assert not user_seen_by_socket('not-a-jwt').is_authenticated
