"""
GET /api/chats/?archived=true must return ONLY the caller's archived
chats. get_queryset() previously just skipped the exclude() when
show_archived was true instead of filtering to archived-only, so the
"الأرشيف" screen silently showed the exact same list as the main inbox.
"""
import pytest
from rest_framework.test import APIClient

from core.models import Category, Item, User

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_item_chat(buyer, seller, title):
    category, _ = Category.objects.get_or_create(name='caf-cat', slug='caf-cat')
    item = Item.objects.create(owner=seller, title=title, description='d', category=category,
                                condition='used', quantity=1, status='active')
    client = APIClient()
    client.force_authenticate(user=buyer)
    response = client.post('/api/chats/create_or_get/', {'item_id': item.id}, format='json')
    return response.json()['data']['id']


def test_archived_view_excludes_non_archived_chats():
    buyer = make_user('caf_buyer1')
    seller = make_user('caf_seller1')
    archived_chat = make_item_chat(buyer, seller, 'archived item')
    active_chat = make_item_chat(buyer, seller, 'active item')

    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f'/api/chats/{archived_chat}/archive/')

    response = client.get('/api/chats/?archived=true')
    ids = [row['id'] for row in response.json()['results']]

    assert archived_chat in ids
    assert active_chat not in ids


def test_default_view_excludes_archived_chats():
    buyer = make_user('caf_buyer2')
    seller = make_user('caf_seller2')
    archived_chat = make_item_chat(buyer, seller, 'archived item 2')
    active_chat = make_item_chat(buyer, seller, 'active item 2')

    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f'/api/chats/{archived_chat}/archive/')

    response = client.get('/api/chats/')
    ids = [row['id'] for row in response.json()['results']]

    assert active_chat in ids
    assert archived_chat not in ids


def test_unarchiving_moves_a_chat_back_to_the_default_view():
    buyer = make_user('caf_buyer3')
    seller = make_user('caf_seller3')
    chat_id = make_item_chat(buyer, seller, 'toggle item')

    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post(f'/api/chats/{chat_id}/archive/')
    client.post(f'/api/chats/{chat_id}/unarchive/')

    default_ids = [row['id'] for row in client.get('/api/chats/').json()['results']]
    archived_ids = [row['id'] for row in client.get('/api/chats/?archived=true').json()['results']]

    assert chat_id in default_ids
    assert chat_id not in archived_ids


def test_archiving_is_per_participant_not_shared():
    """The seller archiving a chat must not hide it from the buyer's inbox."""
    buyer = make_user('caf_buyer4')
    seller = make_user('caf_seller4')
    chat_id = make_item_chat(buyer, seller, 'shared item')

    seller_client = APIClient()
    seller_client.force_authenticate(user=seller)
    seller_client.post(f'/api/chats/{chat_id}/archive/')

    buyer_client = APIClient()
    buyer_client.force_authenticate(user=buyer)
    buyer_ids = [row['id'] for row in buyer_client.get('/api/chats/').json()['results']]

    assert chat_id in buyer_ids
