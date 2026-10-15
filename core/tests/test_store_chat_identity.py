"""
GET /api/chats/ — `is_own_store` (ChatSerializer.get_is_own_store).

A store-level chat's `store` field is always that store's own info,
regardless of who's asking. Without `is_own_store`, the Flutter inbox used
`store.name` as the conversation's display title unconditionally — correct
for the customer (they're talking *to* the store), but wrong for the store
owner, whose inbox would show their own store's name instead of the
customer's. This flag lets the client pick the right side.
"""
import pytest
from rest_framework.test import APIClient

from core.models import Category, Item, User, UserStore

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_store_chat():
    """A customer's direct chat with a store (create_or_get_store)."""
    owner = make_user(f'sci_owner_{id(object())}')
    customer = make_user(f'sci_customer_{id(object())}')
    store = UserStore.objects.create(user=owner, name='متجر الأناقة')

    client = APIClient()
    client.force_authenticate(user=customer)
    response = client.post('/api/chats/create_or_get_store/', {'store_id': store.id}, format='json')
    assert response.status_code == 201
    chat_id = response.json()['data']['id']

    return chat_id, owner, customer, store


def test_customer_view_flags_is_own_store_false():
    chat_id, _owner, customer, _store = make_store_chat()

    client = APIClient()
    client.force_authenticate(user=customer)
    response = client.get('/api/chats/')

    result = response.json()['results'][0]
    assert result['id'] == chat_id
    assert result['is_own_store'] is False


def test_store_owner_view_flags_is_own_store_true():
    chat_id, owner, _customer, _store = make_store_chat()

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get('/api/chats/')

    result = response.json()['results'][0]
    assert result['id'] == chat_id
    assert result['is_own_store'] is True


def test_store_owner_view_still_exposes_store_and_other_user():
    """The client needs both regardless: `other_user` for the owner's title,
    `store` in case a future screen still wants to show which of their
    stores the conversation belongs to."""
    chat_id, owner, customer, store = make_store_chat()

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get('/api/chats/')

    result = response.json()['results'][0]
    assert result['id'] == chat_id
    assert result['store']['id'] == store.id
    assert result['other_user']['username'] == customer.username


# ---------------------------------------------------------------------------
# GET /api/chats/{id}/ (ChatDetailSerializer) — same flags, needed when a
# chat is opened directly by id with no title available yet (e.g. a push
# notification deep link), so the screen can resolve its own title instead
# of showing a blank one until/unless it's passed in from a list screen.
# ---------------------------------------------------------------------------

def test_chat_detail_exposes_store_and_flags_for_customer():
    chat_id, _owner, customer, store = make_store_chat()

    client = APIClient()
    client.force_authenticate(user=customer)
    response = client.get(f'/api/chats/{chat_id}/')

    data = response.json()
    assert data['store']['id'] == store.id
    assert data['is_support'] is False
    assert data['is_own_store'] is False


def test_chat_detail_flags_is_own_store_for_owner():
    chat_id, owner, customer, _store = make_store_chat()

    client = APIClient()
    client.force_authenticate(user=owner)
    response = client.get(f'/api/chats/{chat_id}/')

    data = response.json()
    assert data['is_own_store'] is True
    assert data['other_user']['username'] == customer.username


def test_item_based_chat_is_never_flagged_as_own_store():
    owner = make_user(f'sci_item_owner_{id(object())}')
    buyer = make_user(f'sci_item_buyer_{id(object())}')
    category, _ = Category.objects.get_or_create(name='sci-cat', slug='sci-cat')
    item = Item.objects.create(owner=owner, title='i', description='d', category=category,
                                condition='used', quantity=1, status='active')

    client = APIClient()
    client.force_authenticate(user=buyer)
    create_response = client.post('/api/chats/create_or_get/', {'item_id': item.id}, format='json')
    assert create_response.status_code == 201

    client.force_authenticate(user=owner)
    response = client.get('/api/chats/')
    result = response.json()['results'][0]
    assert result['is_own_store'] is False
