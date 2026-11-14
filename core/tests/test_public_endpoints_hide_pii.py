"""A stranger browsing the marketplace must not see sellers' contact details.

UserSerializer and LocationSerializer are nested read-only inside the public
item/store/auction views. They used to carry the owner's email and phone and
the location's exact address and GPS, so one anonymous GET /api/items/
returned every seller's phone and a pin on their home. Both serializers now
strip those fields for anyone who is not the record's owner (or staff).
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, Location, User,
)

pytestmark = pytest.mark.django_db

SELLER_EMAIL = 'seller@example.com'
SELLER_PHONE = '+9647701112233'


@pytest.fixture
def seller():
    return User.objects.create_user(
        username='seller', password='Str0ng-Pass!9',
        email=SELLER_EMAIL, phone=SELLER_PHONE, phone_verified=True)


@pytest.fixture
def item(seller):
    category, _ = Category.objects.get_or_create(name='pub', slug='pub')
    location = Location.objects.create(
        user=seller, name='بيت', address_line1='حي 123 زقاق 4', city='بغداد',
        state='بغداد', country='IQ', latitude=Decimal('33.3152'),
        longitude=Decimal('44.3661'), contact_phone='07701112233')
    it = Item.objects.create(
        owner=seller, current_holder=seller, title='t', description='d',
        category=category, condition='new', quantity=3, status='active',
        current_location=location)
    FixedPriceListing.objects.create(item=it, price=Decimal('1000'), deposit_percentage=10)
    return it


def owner_block(payload):
    return payload['owner']


def location_block(payload):
    return payload['current_location']


def test_anonymous_item_detail_hides_seller_contact_and_pin(item):
    r = APIClient().get(f'/api/items/{item.id}/')

    assert r.status_code == 200
    owner = owner_block(r.data)
    assert 'email' not in owner and 'phone' not in owner
    # the trust signals a buyer should still see
    assert owner['username'] == 'seller'
    assert 'phone_verified' in owner
    loc = location_block(r.data)
    assert loc['city'] == 'بغداد'  # coarse area stays
    for gone in ('latitude', 'longitude', 'address_line1', 'contact_phone', 'full_address'):
        assert gone not in loc, gone


def test_anonymous_item_list_hides_contact(item):
    r = APIClient().get('/api/items/')
    owner = owner_block(r.data['results'][0])
    assert 'email' not in owner and 'phone' not in owner


def test_the_owner_sees_their_own_contact_and_pin(item, seller):
    c = APIClient()
    c.force_authenticate(user=seller)
    r = c.get(f'/api/items/{item.id}/')

    owner = owner_block(r.data)
    assert owner['email'] == SELLER_EMAIL
    assert owner['phone'] == SELLER_PHONE
    loc = location_block(r.data)
    assert loc['latitude'] is not None
    assert loc['address_line1'] == 'حي 123 زقاق 4'


def test_another_logged_in_user_is_still_a_stranger(item):
    buyer = User.objects.create_user(username='buyer', password='x',
                                     phone='+9647705556677')
    c = APIClient()
    c.force_authenticate(user=buyer)
    r = c.get(f'/api/items/{item.id}/')

    owner = owner_block(r.data)
    assert 'phone' not in owner
    assert 'latitude' not in location_block(r.data)


def test_staff_sees_everything(item):
    admin = User.objects.create_user(username='admin', password='x', is_staff=True)
    c = APIClient()
    c.force_authenticate(user=admin)
    r = c.get(f'/api/items/{item.id}/')

    assert owner_block(r.data)['phone'] == SELLER_PHONE
    assert location_block(r.data)['latitude'] is not None


def test_profile_still_returns_the_users_own_email(seller):
    c = APIClient()
    c.force_authenticate(user=seller)
    r = c.get('/api/auth/profile/')

    assert r.status_code == 200
    assert r.data['email'] == SELLER_EMAIL
    assert r.data['phone'] == SELLER_PHONE
