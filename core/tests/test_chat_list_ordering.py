"""
GET /api/chats/ must return the most recently *active* conversation first
(Message.save() bumps chat.updated_at on every new message) — the inbox
screen relies on this to show the latest activity at the top without doing
its own client-side sort. ChatViewSet.get_queryset() now orders explicitly
rather than relying only on Chat.Meta.ordering.
"""
import pytest
from rest_framework.test import APIClient

from core.models import Category, Item, Message, User

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_item_chat(buyer, seller, title):
    category, _ = Category.objects.get_or_create(name='clo-cat', slug='clo-cat')
    item = Item.objects.create(owner=seller, title=title, description='d', category=category,
                                condition='used', quantity=1, status='active')
    client = APIClient()
    client.force_authenticate(user=buyer)
    response = client.post('/api/chats/create_or_get/', {'item_id': item.id}, format='json')
    return response.json()['data']['id']


def test_most_recently_messaged_chat_is_listed_first():
    buyer = make_user(f'clo_buyer_{id(object())}')
    seller = make_user(f'clo_seller_{id(object())}')

    chat_a = make_item_chat(buyer, seller, 'item A')
    chat_b = make_item_chat(buyer, seller, 'item B')
    chat_c = make_item_chat(buyer, seller, 'item C')
    # Created in order A, B, C — so by creation time alone C would be
    # newest. Bumping A's activity after the fact must move it to the top.
    Message.objects.create(chat_id=chat_a, sender=buyer, message_type='user', content='hi again')

    client = APIClient()
    client.force_authenticate(user=buyer)
    response = client.get('/api/chats/')
    ids_in_order = [row['id'] for row in response.json()['results']]

    assert ids_in_order.index(chat_a) < ids_in_order.index(chat_c)
    assert ids_in_order.index(chat_c) < ids_in_order.index(chat_b)
