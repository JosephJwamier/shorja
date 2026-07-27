"""
Older-message pagination for a chat thread.

ChatDetailSerializer (GET /api/chats/{id}/) only ships the latest
MESSAGES_PAGE_SIZE messages inline; ChatViewSet.messages (GET
/api/chats/{id}/messages/?before=<id>) fetches earlier pages as the user
scrolls up. Also covers the get_queryset() fix that scopes the
archived/non-archived split to the `list` action only — every detail action
(retrieve, messages, ...) must still reach an archived chat.
"""
import pytest
from rest_framework.test import APIClient

from core.models import Category, Chat, Item, Message, User
from core.serializers import MESSAGES_PAGE_SIZE

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_chat():
    owner = make_user(f'pg_owner_{id(object())}')
    buyer = make_user(f'pg_buyer_{id(object())}')
    category, _ = Category.objects.get_or_create(name='pg-cat', slug='pg-cat')
    item = Item.objects.create(owner=owner, title='i', description='d', category=category,
                                condition='good', quantity=1, status='active')
    return Chat.objects.create(item=item, buyer=buyer, seller=owner)


def add_messages(chat, sender, count):
    return [
        Message.objects.create(chat=chat, sender=sender, content=f'm{i}', message_type='user')
        for i in range(count)
    ]


def test_chat_detail_only_returns_latest_page_of_messages():
    chat = make_chat()
    messages = add_messages(chat, chat.buyer, MESSAGES_PAGE_SIZE + 5)

    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    response = client.get(f'/api/chats/{chat.id}/')

    assert response.status_code == 200
    data = response.json()
    assert len(data['messages']) == MESSAGES_PAGE_SIZE
    assert data['has_more_messages'] is True
    # الأحدث فقط: أول 5 رسائل (الأقدم) يجب ألا تظهر
    returned_ids = {m['id'] for m in data['messages']}
    assert messages[0].id not in returned_ids
    assert messages[-1].id in returned_ids


def test_chat_detail_has_more_messages_false_when_under_page_size():
    chat = make_chat()
    add_messages(chat, chat.buyer, 3)

    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    response = client.get(f'/api/chats/{chat.id}/')

    data = response.json()
    assert len(data['messages']) == 3
    assert data['has_more_messages'] is False


def test_messages_action_returns_older_page_before_cursor():
    chat = make_chat()
    messages = add_messages(chat, chat.buyer, MESSAGES_PAGE_SIZE + 5)
    oldest_loaded_id = messages[5].id  # أول رسالة ظاهرة في الصفحة الأحدث

    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    response = client.get(f'/api/chats/{chat.id}/messages/', {'before': oldest_loaded_id})

    assert response.status_code == 200
    body = response.json()
    assert body['success'] is True
    returned_ids = [m['id'] for m in body['data']]
    assert returned_ids == [m.id for m in messages[:5]]
    assert body['has_more'] is False


def test_messages_action_rejects_non_participant():
    chat = make_chat()
    add_messages(chat, chat.buyer, 3)
    outsider = make_user('pg_outsider')

    client = APIClient()
    client.force_authenticate(user=outsider)
    response = client.get(f'/api/chats/{chat.id}/messages/')

    assert response.status_code == 404


def test_messages_action_works_on_archived_chat_without_archived_param():
    chat = make_chat()
    add_messages(chat, chat.buyer, 3)
    chat.is_archived_by_buyer = True
    chat.save(update_fields=['is_archived_by_buyer'])

    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    response = client.get(f'/api/chats/{chat.id}/messages/')

    assert response.status_code == 200


def test_retrieve_archived_chat_without_archived_param_succeeds():
    chat = make_chat()
    chat.is_archived_by_buyer = True
    chat.save(update_fields=['is_archived_by_buyer'])

    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    response = client.get(f'/api/chats/{chat.id}/')

    assert response.status_code == 200


def test_list_still_excludes_archived_chats_by_default():
    chat = make_chat()
    chat.is_archived_by_buyer = True
    chat.save(update_fields=['is_archived_by_buyer'])

    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    response = client.get('/api/chats/')

    assert response.status_code == 200
    assert response.json()['results'] == []


def test_list_includes_archived_chats_with_archived_param():
    chat = make_chat()
    chat.is_archived_by_buyer = True
    chat.save(update_fields=['is_archived_by_buyer'])

    client = APIClient()
    client.force_authenticate(user=chat.buyer)
    response = client.get('/api/chats/', {'archived': 'true'})

    assert response.status_code == 200
    results = response.json()['results']
    assert len(results) == 1
    assert results[0]['id'] == chat.id
