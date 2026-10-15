"""Guest browsing: a visitor with no account can look, but cannot act.

Opening read endpoints is the easy half. The half that breaks in production
is serializers that assume request.user exists — AnonymousUser is truthy, so
a `if request.user:` guard passes and the next attribute access raises. These
tests exercise each open route with a genuinely anonymous client so that
failure shows up here rather than on a stranger's phone.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from core.models import Category, Item

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def guest():
    """No credentials at all — not a logged-out user, an unknown visitor."""
    return APIClient()


@pytest.fixture
def seller():
    return User.objects.create_user(
        username='seller', password='x', phone='07701234567')


@pytest.fixture
def item(seller):
    category = Category.objects.create(name='Phones', name_ar='هواتف')
    return Item.objects.create(
        owner=seller, title='Galaxy', description='d',
        category=category, condition='new', status='available')


class TestGuestCanLook:
    def test_item_list(self, guest, item):
        r = guest.get('/api/items/')
        assert r.status_code == 200, r.data

    def test_item_detail(self, guest, item):
        r = guest.get(f'/api/items/{item.id}/')
        assert r.status_code == 200, r.data

    def test_item_detail_reports_not_favorited_rather_than_crashing(self, guest, item):
        # get_is_favorited() must short-circuit on AnonymousUser instead of
        # querying favorited_by with it.
        r = guest.get(f'/api/items/{item.id}/')
        assert r.data.get('is_favorited') is False

    def test_categories(self, guest, item):
        assert guest.get('/api/categories/').status_code == 200

    def test_auctions(self, guest):
        assert guest.get('/api/auctions/').status_code == 200

    def test_stores(self, guest):
        assert guest.get('/api/stores/').status_code == 200

    def test_reels(self, guest):
        assert guest.get('/api/reels/').status_code == 200


class TestGuestCannotAct:
    """The point of opening reads is that writes stay shut."""

    def test_cannot_create_an_item(self, guest):
        r = guest.post('/api/items/', {'title': 'x'}, format='json')
        assert r.status_code == 401

    def test_cannot_see_someone_elses_auctions(self, guest):
        assert guest.get('/api/auctions/my_auctions/').status_code == 401

    def test_cannot_reserve(self, guest, item):
        r = guest.post('/api/reservations/', {'item': item.id}, format='json')
        assert r.status_code == 401

    def test_cannot_read_chats(self, guest):
        assert guest.get('/api/chats/').status_code == 401

    def test_cannot_read_a_profile(self, guest):
        assert guest.get('/api/auth/profile/').status_code == 401
