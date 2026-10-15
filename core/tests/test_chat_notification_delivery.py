"""
Chat actions were only broadcasting to `ws/notifications/` — a channel the
Flutter app never actually listens on (it's FCM-only for live pushes, see
notification_service.dart). So sending an image, sending a price offer, and
accepting/rejecting one never produced a real notification: no push, and no
row in the in-app notifications feed either, since that dead broadcast never
wrote a `Notification`. Only plain-text messages sent over the chat
WebSocket (ChatConsumer.handle_chat_message -> notify_new_message) ever did.

These tests assert the concrete, observable side effect of a real
notification: a `Notification` row for the recipient with the right type —
Firebase itself is not asserted (FCM only fires when a token happens to be
configured; the row is written unconditionally by notify_push).
"""
import base64

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from core.models import Category, Item, Message, Notification, User, UserStore

pytestmark = pytest.mark.django_db

# Smallest possible valid PNG (1x1 transparent pixel) — DRF's ImageField
# validates uploads with Pillow, so arbitrary bytes are rejected.
_PNG_1X1 = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_item_chat():
    owner = make_user(f'cnd_owner_{id(object())}')
    buyer = make_user(f'cnd_buyer_{id(object())}')
    category, _ = Category.objects.get_or_create(name='cnd-cat', slug='cnd-cat')
    item = Item.objects.create(owner=owner, title='i', description='d', category=category,
                                condition='used', quantity=5, status='active')

    client = APIClient()
    client.force_authenticate(user=buyer)
    response = client.post('/api/chats/create_or_get/', {'item_id': item.id}, format='json')
    assert response.status_code == 201
    chat_id = response.json()['data']['id']
    return client, chat_id, owner, buyer, item


def test_image_message_notifies_recipient():
    client, chat_id, owner, buyer, _item = make_item_chat()

    image = SimpleUploadedFile('photo.png', _PNG_1X1, content_type='image/png')
    response = client.post(
        f'/api/chats/{chat_id}/send_message/', {'content': '', 'attachment': image}, format='multipart',
    )
    assert response.status_code == 201

    assert Notification.objects.filter(user=owner, notification_type='new_message').exists()


def test_offer_notifies_seller():
    client, chat_id, owner, _buyer, _item = make_item_chat()

    response = client.post(
        f'/api/chats/{chat_id}/send_offer/', {'offer_amount': '5.00', 'quantity': 1}, format='json',
    )
    assert response.status_code == 201

    assert Notification.objects.filter(user=owner, notification_type='new_message').exists()


def test_accept_offer_notifies_buyer():
    client, chat_id, owner, buyer, _item = make_item_chat()
    offer_response = client.post(
        f'/api/chats/{chat_id}/send_offer/', {'offer_amount': '5.00', 'quantity': 1}, format='json',
    )
    message_id = offer_response.json()['data']['id']

    seller_client = APIClient()
    seller_client.force_authenticate(user=owner)
    response = seller_client.post(f'/api/messages/{message_id}/accept_offer/')
    assert response.status_code == 200

    assert Notification.objects.filter(user=buyer, notification_type='new_message').exists()


def test_reject_offer_notifies_buyer():
    """Previously reject_offer had no notification block at all — not even
    the dead websocket-only one accept_offer had."""
    client, chat_id, owner, buyer, _item = make_item_chat()
    offer_response = client.post(
        f'/api/chats/{chat_id}/send_offer/', {'offer_amount': '5.00', 'quantity': 1}, format='json',
    )
    message_id = offer_response.json()['data']['id']

    seller_client = APIClient()
    seller_client.force_authenticate(user=owner)
    response = seller_client.post(f'/api/messages/{message_id}/reject_offer/')
    assert response.status_code == 200

    assert Notification.objects.filter(user=buyer, notification_type='new_message').exists()


def test_store_chat_text_message_still_notifies_via_rest_send_message():
    """Sanity check unrelated to the fix: a plain-text message sent through
    the REST send_message path (not the websocket) also notifies — it goes
    through the same code path as the image case above."""
    owner = make_user(f'cnd_owner2_{id(object())}')
    buyer = make_user(f'cnd_buyer2_{id(object())}')
    store = UserStore.objects.create(user=owner, name='cnd store')

    client = APIClient()
    client.force_authenticate(user=buyer)
    chat_response = client.post('/api/chats/create_or_get_store/', {'store_id': store.id}, format='json')
    chat_id = chat_response.json()['data']['id']

    response = client.post(f'/api/chats/{chat_id}/send_message/', {'content': 'hello'}, format='json')
    assert response.status_code == 201
    assert Message.objects.filter(chat_id=chat_id, content='hello').exists()
    assert Notification.objects.filter(user=owner, notification_type='new_message').exists()
