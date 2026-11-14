"""A stranger browsing the marketplace must not see sellers' contact details.

UserSerializer and LocationSerializer are nested read-only inside the public
item/store/auction views. They used to carry the owner's email and phone and
the location's street address and contact phone, so one anonymous GET
/api/items/ returned every seller's phone and where they live. Now, for anyone
who is not the record's owner (or staff):

- a user loses email, phone and date of birth;
- an item's location loses its street address, label and on-site contact;
  its exact pin stays, because the "near me" map places each item where it
  is (the owner's decision, 2026-11-15);
- a store's location stays public (the store page exists to show where the
  shop is), minus the on-site contact and notes;
- a reservation's delivery address stays whole for the reservation's parties,
  because the seller has to know where the order goes.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, Location, Reservation, User, UserStore,
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


def test_anonymous_item_detail_hides_seller_contact_and_address(item):
    r = APIClient().get(f'/api/items/{item.id}/')

    assert r.status_code == 200
    owner = owner_block(r.data)
    assert 'email' not in owner and 'phone' not in owner
    # the trust signals a buyer should still see
    assert owner['username'] == 'seller'
    assert 'phone_verified' in owner
    loc = location_block(r.data)
    assert loc['city'] == 'بغداد'  # coarse area stays
    for gone in ('name', 'address_line1', 'contact_phone', 'full_address'):
        assert gone not in loc, gone
    # The exact pin stays for the map.
    assert loc['latitude'] == '33.315200'
    assert loc['longitude'] == '44.366100'


def test_the_near_me_map_still_gets_a_pin_for_every_item(item):
    # MapScreen skips any item without coordinates: stripping them outright
    # emptied the map for everyone but the owner.
    r = APIClient().get('/api/items/')
    loc = r.data['results'][0]['current_location']
    assert loc['latitude'] is not None and loc['longitude'] is not None


def test_a_store_location_is_public_but_its_contact_is_not(seller):
    shop_front = Location.objects.create(
        user=seller, name='المحل', address_line1='شارع الرشيد', city='بغداد',
        state='بغداد', country='IQ', latitude=Decimal('33.3412'),
        longitude=Decimal('44.4009'), contact_phone='07701112233',
        notes='المفتاح عند الجار')
    UserStore.objects.create(user=seller, name='متجر',
                             default_location=shop_front, is_active=True)

    r = APIClient().get('/api/stores/public/seller/')

    assert r.status_code == 200, r.data
    loc = r.data['data']['default_location']
    assert loc['address_line1'] == 'شارع الرشيد'
    assert loc['full_address']
    assert loc['latitude'] == '33.341200'      # exact: it is a shop front
    assert 'contact_phone' not in loc and 'notes' not in loc


def _reservation(item, buyer):
    home = Location.objects.create(
        user=buyer, name='بيتي', address_line1='حي الجامعة دار 9', city='بغداد',
        state='بغداد', country='IQ', latitude=Decimal('33.2777'),
        longitude=Decimal('44.3888'), contact_phone='07705556677')
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1, deposit_amount=Decimal('100'),
        total_price=Decimal('1000'), status='completed',
        expires_at=timezone.now() + timedelta(days=1), delivery_location=home)


def test_the_seller_sees_where_the_order_goes(item, seller):
    buyer = User.objects.create_user(username='buyer2', password='x', phone='+9647705556677')
    reservation = _reservation(item, buyer)
    c = APIClient()
    c.force_authenticate(user=seller)

    r = c.get(f'/api/reservations/{reservation.id}/')

    assert r.status_code == 200, r.data
    loc = r.data['delivery_location']
    assert loc['address_line1'] == 'حي الجامعة دار 9'
    assert loc['latitude'] == '33.277700'
    assert loc['contact_phone'] == '07705556677'
    # ...but not the buyer's own account details.
    assert 'phone' not in r.data['buyer']


def test_the_buyer_sees_their_own_delivery_address(item):
    buyer = User.objects.create_user(username='buyer3', password='x')
    reservation = _reservation(item, buyer)
    c = APIClient()
    c.force_authenticate(user=buyer)

    r = c.get(f'/api/reservations/{reservation.id}/')

    assert r.data['delivery_location']['address_line1'] == 'حي الجامعة دار 9'


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
    assert 'address_line1' not in location_block(r.data)
    assert location_block(r.data)['latitude'] == '33.315200'


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
