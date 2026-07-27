"""
"التواصل مع الدعم" — in-app support chat feature.

Covers both halves: the DRF endpoint every user hits from the Flutter app
(core/views.py ChatViewSet.create_or_get_support, backed by
core/services/support.py::start_support_chat) and the admin-only reply
console staff use to answer it (core/admin.py ChatAdmin.reply_view). The
fixed support account itself is created by migration
0045_create_support_account, which runs automatically in the test DB.
"""
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import Category, Chat, Item, Message, User
from core.services.support import SUPPORT_USERNAME, start_support_chat

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_staff(username, group_name):
    user = User.objects.create_user(username=username, password='pass12345', is_staff=True)
    user.groups.add(Group.objects.get(name=group_name))
    return user


def make_regular_chat():
    """A normal buyer/seller chat about an item — never support-repliable."""
    owner = make_user(f'sc_owner_{id(object())}')
    buyer = make_user(f'sc_buyer_{id(object())}')
    category, _ = Category.objects.get_or_create(name='sc-cat', slug='sc-cat')
    item = Item.objects.create(owner=owner, title='i', description='d', category=category,
                                condition='good', quantity=1, status='active')
    return Chat.objects.create(item=item, buyer=buyer, seller=owner)


# ---------------------------------------------------------------------------
# DRF endpoint — POST /api/chats/create_or_get_support/
# ---------------------------------------------------------------------------

def test_create_or_get_support_creates_chat_with_fixed_support_account():
    user = make_user('sup_user1')
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post('/api/chats/create_or_get_support/', {}, format='json')

    assert response.status_code == 201
    body = response.json()
    assert body['success'] is True

    chat = Chat.objects.get(buyer=user, seller__username=SUPPORT_USERNAME)
    assert chat.item_id is None
    assert chat.store_id is None
    assert body['data']['id'] == chat.id


def test_create_or_get_support_is_idempotent():
    user = make_user('sup_user2')
    client = APIClient()
    client.force_authenticate(user=user)

    first = client.post('/api/chats/create_or_get_support/', {}, format='json')
    second = client.post('/api/chats/create_or_get_support/', {}, format='json')

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()['data']['id'] == second.json()['data']['id']
    assert Chat.objects.filter(buyer=user, seller__username=SUPPORT_USERNAME).count() == 1


def test_create_or_get_support_with_initial_message_creates_message():
    user = make_user('sup_user3')
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.post('/api/chats/create_or_get_support/',
                            {'initial_message': 'أحتاج مساعدة'}, format='json')

    assert response.status_code == 201
    chat = Chat.objects.get(buyer=user, seller__username=SUPPORT_USERNAME)
    assert Message.objects.filter(chat=chat, sender=user, content='أحتاج مساعدة').exists()


def test_create_or_get_support_does_not_duplicate_initial_message_on_refetch():
    user = make_user('sup_user4')
    client = APIClient()
    client.force_authenticate(user=user)

    client.post('/api/chats/create_or_get_support/', {'initial_message': 'مرحبا'}, format='json')
    response = client.post('/api/chats/create_or_get_support/', {'initial_message': 'مرحبا'}, format='json')

    assert response.status_code == 200
    chat = Chat.objects.get(buyer=user, seller__username=SUPPORT_USERNAME)
    assert Message.objects.filter(chat=chat).count() == 1


def test_create_or_get_support_reactivates_chat_archived_by_buyer():
    user = make_user('sup_user5')
    chat, _ = start_support_chat(user)
    chat.is_archived_by_buyer = True
    chat.save(update_fields=['is_archived_by_buyer'])

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post('/api/chats/create_or_get_support/', {}, format='json')

    assert response.status_code == 200
    chat.refresh_from_db()
    assert chat.is_archived_by_buyer is False


def test_create_or_get_support_requires_authentication():
    client = APIClient()
    response = client.post('/api/chats/create_or_get_support/', {}, format='json')
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/chats/ — `is_support` flag (ChatSerializer.get_is_support), used
# by the Flutter inbox to give the support chat its own title/icon instead
# of falling back to the fixed support account's raw username.
# ---------------------------------------------------------------------------

def test_chat_list_flags_support_chat_with_is_support():
    user = make_user('sup_list1')
    start_support_chat(user)

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get('/api/chats/')

    assert response.status_code == 200
    results = response.json()['results']
    assert len(results) == 1
    assert results[0]['is_support'] is True


def test_chat_list_does_not_flag_regular_chat_as_support():
    chat = make_regular_chat()
    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    response = client.get('/api/chats/')

    assert response.status_code == 200
    results = response.json()['results']
    assert len(results) == 1
    assert results[0]['is_support'] is False


# ---------------------------------------------------------------------------
# Admin reply console — GET/POST admin:core_chat_reply
# ---------------------------------------------------------------------------

def test_reply_view_get_succeeds_for_support_group():
    user = make_user('rv_user1')
    chat, _ = start_support_chat(user)
    staff = make_staff('rv_staff1', 'الدعم الفني')

    client = Client()
    client.force_login(staff)
    response = client.get(reverse('admin:core_chat_reply', args=[chat.pk]))

    assert response.status_code == 200


def test_reply_view_get_succeeds_for_superuser():
    user = make_user('rv_user2')
    chat, _ = start_support_chat(user)
    superuser = User.objects.create_superuser(username='rv_super1', password='x', email='a@a.com')

    client = Client()
    client.force_login(superuser)
    response = client.get(reverse('admin:core_chat_reply', args=[chat.pk]))

    assert response.status_code == 200


def test_reply_view_rejects_group_without_chat_view_permission():
    user = make_user('rv_user3')
    chat, _ = start_support_chat(user)
    staff = make_staff('rv_staff3', 'عمليات التوصيل')  # Delivery Ops — no view_chat

    client = Client()
    client.force_login(staff)
    response = client.get(reverse('admin:core_chat_reply', args=[chat.pk]))

    assert response.status_code == 403


def test_reply_view_rejects_non_support_chat():
    chat = make_regular_chat()
    staff = make_staff('rv_staff4', 'الدعم الفني')

    client = Client()
    client.force_login(staff)
    response = client.get(reverse('admin:core_chat_reply', args=[chat.pk]))

    assert response.status_code == 403


def test_reply_view_requires_login():
    user = make_user('rv_user5')
    chat, _ = start_support_chat(user)

    client = Client()
    response = client.get(reverse('admin:core_chat_reply', args=[chat.pk]))

    assert response.status_code == 302
    assert '/login/' in response.url or reverse('admin:login') in response.url


@patch('core.consumers.broadcast_chat_message')
def test_reply_view_post_creates_message_and_broadcasts(mock_broadcast):
    user = make_user('rv_user6')
    chat, _ = start_support_chat(user)
    staff = make_staff('rv_staff6', 'الدعم الفني')

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_chat_reply', args=[chat.pk]), {'content': 'رد الدعم'})

    assert response.status_code == 302
    message = Message.objects.get(chat=chat, sender=staff)
    assert message.content == 'رد الدعم'
    assert message.message_type == 'user'
    mock_broadcast.assert_called_once_with(message)


def test_reply_view_post_empty_content_redisplays_form_without_creating_message():
    user = make_user('rv_user7')
    chat, _ = start_support_chat(user)
    staff = make_staff('rv_staff7', 'الدعم الفني')

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_chat_reply', args=[chat.pk]), {'content': '   '})

    assert response.status_code == 200
    assert not Message.objects.filter(chat=chat).exists()


@patch('core.consumers.broadcast_chat_message', side_effect=RuntimeError('no channel layer'))
def test_reply_view_post_survives_broadcast_failure(mock_broadcast):
    user = make_user('rv_user8')
    chat, _ = start_support_chat(user)
    staff = make_staff('rv_staff8', 'الدعم الفني')

    client = Client()
    client.force_login(staff)
    response = client.post(reverse('admin:core_chat_reply', args=[chat.pk]), {'content': 'رد رغم الخطأ'})

    assert response.status_code == 302
    assert Message.objects.filter(chat=chat, content='رد رغم الخطأ').exists()
