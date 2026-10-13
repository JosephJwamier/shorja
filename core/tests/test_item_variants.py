"""Product variants — colour / size / etc. on one item.

The property under test throughout: stock lives on the variant, the item's
quantity is a derived total, and every path that moves stock (reservation,
completion, cancellation, wholesale transfer) moves it on the right variant.
Getting this wrong sells a red shirt that doesn't exist because blue ones do.
"""
from datetime import timedelta
from decimal import Decimal

import json

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, ItemTransfer, ItemVariant, Location,
    Reservation, User, UserStore, get_or_create_wallet,
)
from core.wallet_utils import complete_reservation

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_category():
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return category


def make_item(owner, quantity=10, price='100.00', status='active', **extra):
    item = Item.objects.create(
        owner=owner, title='shirt', description='d', category=make_category(),
        condition='new', quantity=quantity, status=status, **extra
    )
    FixedPriceListing.objects.create(item=item, price=Decimal(price), deposit_percentage=10)
    return item


def make_optioned_item(owner, price='100.00'):
    """A shirt in two colours; red is nearly out, blue is plentiful."""
    item = make_item(owner, quantity=0, price=price,
                     options=[{'name': 'اللون', 'values': ['أحمر', 'أزرق']}])
    item.variants.all().delete()
    red = ItemVariant.objects.create(item=item, attributes={'اللون': 'أحمر'}, quantity=1)
    blue = ItemVariant.objects.create(item=item, attributes={'اللون': 'أزرق'},
                                      quantity=5, price=Decimal('120.00'))
    item.sync_stock_from_variants()
    item.refresh_from_db()
    return item, red, blue


def make_location(user):
    return Location.objects.create(
        user=user, name='home', address_line1='x', city='Baghdad', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1',
    )


@pytest.fixture
def client():
    return APIClient()


# ---------------------------------------------------------------------------
# The invariant: every item has a variant
# ---------------------------------------------------------------------------

def test_plain_item_gets_a_default_variant_holding_its_stock():
    item = make_item(make_user('inv1'), quantity=7)
    variants = list(item.variants.all())
    assert len(variants) == 1
    assert variants[0].attributes == {}
    assert variants[0].quantity == 7
    assert item.default_variant == variants[0]


def test_item_quantity_is_the_sum_of_active_variants():
    item, red, blue = make_optioned_item(make_user('inv2'))
    assert item.quantity == 6
    blue.is_active = False
    blue.save()
    item.sync_stock_from_variants()
    assert item.quantity == 1


def test_sold_out_variants_flip_item_to_sold_and_back():
    item = make_item(make_user('inv3'), quantity=1)
    variant = item.default_variant
    variant.take_stock(1)
    item.refresh_from_db()
    assert item.status == 'sold'
    variant.return_stock(1)
    item.refresh_from_db()
    assert item.status == 'active'


def test_take_stock_refuses_to_overdraw():
    item = make_item(make_user('inv4'), quantity=2)
    with pytest.raises(ValueError):
        item.default_variant.take_stock(3)
    item.refresh_from_db()
    assert item.quantity == 2


# ---------------------------------------------------------------------------
# Creating items through the API
# ---------------------------------------------------------------------------

def test_api_create_without_options_keeps_the_old_behaviour(client):
    owner = make_user('api1')
    make_location(owner)   # listing requires a pickup address
    client.force_authenticate(user=owner)
    response = client.post('/api/items/', {
        'title': 't', 'description': 'd', 'category': make_category().id,
        'condition': 'new', 'quantity': 4,
    }, format='json')
    assert response.status_code == 201, response.data
    item = Item.objects.get(id=response.data['id'] if 'id' in response.data else response.data['data']['id'])
    assert item.variants.count() == 1
    assert item.default_variant.quantity == 4
    assert item.quantity == 4


def test_api_create_with_options_builds_the_variants(client):
    owner = make_user('api2')
    make_location(owner)   # listing requires a pickup address
    client.force_authenticate(user=owner)
    response = client.post('/api/items/', {
        'title': 't', 'description': 'd', 'category': make_category().id,
        'condition': 'new', 'quantity': 0,
        'options': [{'name': 'المقاس', 'values': ['M', 'L']}],
        'variants': [
            {'attributes': {'المقاس': 'M'}, 'quantity': 3},
            {'attributes': {'المقاس': 'L'}, 'quantity': 2, 'price': '150.00'},
        ],
    }, format='json')
    assert response.status_code == 201, response.data
    data = response.data.get('data', response.data)
    item = Item.objects.get(id=data['id'])
    assert item.variants.count() == 2
    assert item.quantity == 5
    assert item.variants.get(attributes={'المقاس': 'L'}).price == Decimal('150.00')


def test_api_create_accepts_json_strings_in_multipart(client):
    """The Flutter add-item screen posts multipart (images ride along), so
    `options`/`variants` arrive as JSON strings, not nested JSON. The
    serializer must decode them — this pins that wire shape, including a
    numeric price sent as a float."""
    owner = make_user('api2m')
    make_location(owner)   # listing requires a pickup address
    client.force_authenticate(user=owner)
    response = client.post('/api/items/', {
        'title': 't', 'description': 'd', 'category': make_category().id,
        'condition': 'new', 'quantity': 0,
        'options': json.dumps([{'name': 'اللون', 'values': ['أحمر', 'أزرق']}]),
        'variants': json.dumps([
            {'attributes': {'اللون': 'أحمر'}, 'quantity': 1},
            {'attributes': {'اللون': 'أزرق'}, 'quantity': 2, 'price': 99.0},
        ]),
    }, format='multipart')
    assert response.status_code == 201, response.data
    data = response.data.get('data', response.data)
    item = Item.objects.get(id=data['id'])
    assert item.options == [{'name': 'اللون', 'values': ['أحمر', 'أزرق']}]
    assert item.variants.count() == 2
    assert item.quantity == 3
    assert item.variants.get(attributes={'اللون': 'أزرق'}).price == Decimal('99')


def test_api_rejects_a_variant_for_an_undeclared_value(client):
    owner = make_user('api3')
    make_location(owner)   # listing requires a pickup address
    client.force_authenticate(user=owner)
    response = client.post('/api/items/', {
        'title': 't', 'description': 'd', 'category': make_category().id,
        'condition': 'new', 'quantity': 0,
        'options': [{'name': 'المقاس', 'values': ['M', 'L']}],
        'variants': [{'attributes': {'المقاس': 'XL'}, 'quantity': 3}],
    }, format='json')
    assert response.status_code == 400
    assert not Item.objects.filter(title='t').exists()


def test_api_rejects_duplicate_variants(client):
    owner = make_user('api4')
    make_location(owner)   # listing requires a pickup address
    client.force_authenticate(user=owner)
    response = client.post('/api/items/', {
        'title': 't', 'description': 'd', 'category': make_category().id,
        'condition': 'new', 'quantity': 0,
        'options': [{'name': 'المقاس', 'values': ['M']}],
        'variants': [{'attributes': {'المقاس': 'M'}, 'quantity': 1},
                     {'attributes': {'المقاس': 'M'}, 'quantity': 2}],
    }, format='json')
    assert response.status_code == 400


def test_item_detail_exposes_options_and_variants(client):
    owner = make_user('api5')
    item, red, blue = make_optioned_item(owner)
    client.force_authenticate(user=owner)
    response = client.get(f'/api/items/{item.id}/')
    assert response.status_code == 200
    data = response.data.get('data', response.data)
    assert data['options'] == [{'name': 'اللون', 'values': ['أحمر', 'أزرق']}]
    by_colour = {v['attributes']['اللون']: v for v in data['variants']}
    assert by_colour['أزرق']['effective_price'] == '120.00' or by_colour['أزرق']['effective_price'] == Decimal('120.00')
    assert str(by_colour['أحمر']['effective_price']) == '100.00'


# ---------------------------------------------------------------------------
# Reserving
# ---------------------------------------------------------------------------

def reserve(client, item, variant_id=None, quantity=1):
    body = {'item_id': item.id, 'quantity': quantity}
    if variant_id is not None:
        body['variant_id'] = variant_id
    return client.post('/api/reservations/create_reservation/', body, format='json')


def test_reserving_a_plain_item_needs_no_variant_id(client):
    owner, buyer = make_user('res1o'), make_user('res1b')
    item = make_item(owner, quantity=3)
    client.force_authenticate(user=buyer)
    response = reserve(client, item)
    assert response.status_code == 201, response.data
    reservation = Reservation.objects.get(buyer=buyer)
    assert reservation.variant == item.default_variant


def test_reserving_an_optioned_item_requires_a_variant(client):
    owner, buyer = make_user('res2o'), make_user('res2b')
    item, red, blue = make_optioned_item(owner)
    client.force_authenticate(user=buyer)
    response = reserve(client, item)
    assert response.status_code == 400
    assert not Reservation.objects.filter(buyer=buyer).exists()


def test_stock_is_checked_on_the_chosen_variant_not_the_item_total(client):
    """Six in stock overall; red has one. Two reds must be refused even
    though the item's total says there is plenty."""
    owner, buyer = make_user('res3o'), make_user('res3b')
    item, red, blue = make_optioned_item(owner)
    client.force_authenticate(user=buyer)
    response = reserve(client, item, variant_id=red.id, quantity=2)
    assert response.status_code == 400
    assert 'Only 1 available' in response.data['message']


def test_variant_from_another_item_is_refused(client):
    owner, buyer = make_user('res4o'), make_user('res4b')
    item, red, blue = make_optioned_item(owner)
    other, other_red, _ = make_optioned_item(owner)
    client.force_authenticate(user=buyer)
    response = reserve(client, item, variant_id=other_red.id)
    assert response.status_code == 400


def test_reservation_deposit_follows_the_variant_price(client):
    """Found on the device: a 150-IQD variant under a 120,000-IQD listing
    was asking a 12,000-IQD deposit — the listing's per-unit deposit was
    multiplied straight through. The percentage must apply to the price
    actually charged."""
    owner, buyer = make_user('dep1o'), make_user('dep1b')
    item, red, blue = make_optioned_item(owner, price='120000.00')
    listing = item.fixed_listing
    listing.deposit_percentage = Decimal('10'); listing.save()
    blue.price = Decimal('150.00'); blue.save()
    client.force_authenticate(user=buyer)
    response = reserve(client, item, variant_id=blue.id, quantity=2)
    assert response.status_code == 201, response.data
    reservation = Reservation.objects.get(buyer=buyer)
    assert reservation.total_price == Decimal('300.00')
    assert reservation.deposit_amount == Decimal('30.00')

    # No override → exactly the legacy figure, so plain items are untouched.
    other = make_user('dep1c')
    client.force_authenticate(user=other)
    assert reserve(client, item, variant_id=red.id, quantity=1).status_code == 201
    legacy = Reservation.objects.get(buyer=other)
    assert legacy.deposit_amount == listing.deposit_amount


def test_reservation_total_uses_the_variant_price(client):
    owner, buyer = make_user('res5o'), make_user('res5b')
    item, red, blue = make_optioned_item(owner, price='100.00')
    client.force_authenticate(user=buyer)
    response = reserve(client, item, variant_id=blue.id, quantity=2)
    assert response.status_code == 201, response.data
    reservation = Reservation.objects.get(buyer=buyer)
    assert reservation.total_price == Decimal('240.00')   # 2 × 120, not 2 × 100


def test_deactivated_variant_cannot_be_reserved(client):
    owner, buyer = make_user('res6o'), make_user('res6b')
    item, red, blue = make_optioned_item(owner)
    blue.is_active = False
    blue.save()
    client.force_authenticate(user=buyer)
    assert reserve(client, item, variant_id=blue.id).status_code == 400


# ---------------------------------------------------------------------------
# Completing, cancelling — stock moves on the right variant
# ---------------------------------------------------------------------------

def make_pending_reservation(item, variant, buyer, quantity=1):
    return Reservation.objects.create(
        item=item, variant=variant, buyer=buyer, quantity=quantity,
        deposit_amount=Decimal('10.00'), total_price=Decimal('100.00'),
        status='pending', expires_at=timezone.now() + timedelta(days=1),
        delivery_location=make_location(buyer),
    )


def test_completion_takes_stock_from_the_reserved_variant_only():
    owner, buyer = make_user('cmp1o'), make_user('cmp1b')
    get_or_create_wallet(buyer).credit(Decimal('1000.00'))
    item, red, blue = make_optioned_item(owner)
    reservation = make_pending_reservation(item, blue, buyer, quantity=2)

    complete_reservation(reservation, 'full')

    red.refresh_from_db(); blue.refresh_from_db(); item.refresh_from_db()
    assert blue.quantity == 3
    assert red.quantity == 1
    assert item.quantity == 4
    assert item.sold_count == 2


def test_cancelling_a_completed_reservation_returns_stock_to_its_variant(client):
    owner, buyer = make_user('cmp2o'), make_user('cmp2b')
    get_or_create_wallet(buyer).credit(Decimal('1000.00'))
    item, red, blue = make_optioned_item(owner)
    reservation = make_pending_reservation(item, blue, buyer, quantity=2)
    complete_reservation(reservation, 'full')
    blue.refresh_from_db()
    assert blue.quantity == 3

    client.force_authenticate(user=buyer)
    response = client.post(f'/api/reservations/{reservation.id}/cancel/')
    assert response.status_code == 200, response.data

    blue.refresh_from_db(); red.refresh_from_db()
    assert blue.quantity == 5
    assert red.quantity == 1


def test_legacy_reservation_without_variant_uses_the_default_one():
    """Rows created before variants existed carry variant=None and must keep
    settling against the same stock they always did."""
    owner, buyer = make_user('leg1o'), make_user('leg1b')
    get_or_create_wallet(buyer).credit(Decimal('1000.00'))
    item = make_item(owner, quantity=5)
    reservation = Reservation.objects.create(
        item=item, variant=None, buyer=buyer, quantity=2,
        deposit_amount=Decimal('10.00'), total_price=Decimal('100.00'),
        status='pending', expires_at=timezone.now() + timedelta(days=1),
        delivery_location=make_location(buyer),
    )
    complete_reservation(reservation, 'full')
    item.refresh_from_db()
    assert item.quantity == 3
    assert item.default_variant.quantity == 3


# ---------------------------------------------------------------------------
# Wholesale transfer
# ---------------------------------------------------------------------------

def test_transfer_takes_stock_from_the_named_variant_and_copies_its_label():
    owner, reseller = make_user('tr1o'), make_user('tr1r')
    UserStore.objects.create(user=owner, name='s1')
    UserStore.objects.create(user=reseller, name='s2')
    get_or_create_wallet(reseller).credit(Decimal('10000.00'))
    item, red, blue = make_optioned_item(owner)

    transfer = ItemTransfer.objects.create(
        from_user=owner, to_user=reseller, item=item, variant=blue, quantity=3,
    )
    transfer.activate()

    blue.refresh_from_db(); red.refresh_from_db(); item.refresh_from_db()
    assert blue.quantity == 2
    assert red.quantity == 1
    assert item.quantity == 3

    copy = transfer.resulting_item
    assert copy.variants.count() == 1
    assert copy.default_variant.attributes == {'اللون': 'أزرق'}
    assert copy.default_variant.quantity == 3
    assert copy.quantity == 3
    # Wholesale is struck on the variant's own price.
    assert transfer.wholesale_price_per_unit == Decimal('120.00')


def test_transfer_refuses_more_than_the_variant_holds():
    owner, reseller = make_user('tr2o'), make_user('tr2r')
    UserStore.objects.create(user=owner, name='s1')
    UserStore.objects.create(user=reseller, name='s2')
    get_or_create_wallet(reseller).credit(Decimal('10000.00'))
    item, red, blue = make_optioned_item(owner)

    transfer = ItemTransfer.objects.create(
        from_user=owner, to_user=reseller, item=item, variant=red, quantity=2,
    )
    with pytest.raises(ValueError):
        transfer.activate()
    red.refresh_from_db()
    assert red.quantity == 1


# ---------------------------------------------------------------------------
# Editing items through the API (the seller's edit screen PATCHes here)
# ---------------------------------------------------------------------------

def test_editing_quantity_on_a_plain_item_restocks_its_default_variant(client):
    """The edit screen has always sent `quantity`. It must land on the variant
    the stock code reads, not just on the denormalised total — otherwise a
    seller restocking a sold-out item sees it relisted while every
    reservation still fails against an empty variant."""
    owner = make_user('upd1')
    item = make_item(owner, quantity=0, status='sold')
    client.force_authenticate(user=owner)
    response = client.patch(f'/api/items/{item.id}/', {'quantity': 10}, format='multipart')
    assert response.status_code == 200, response.data
    item.refresh_from_db()
    assert item.default_variant.quantity == 10
    assert item.quantity == 10
    assert item.status == 'active'


def test_editing_options_resends_the_whole_table(client):
    owner, buyer = make_user('upd2o'), make_user('upd2b')
    item, red, blue = make_optioned_item(owner)
    client.force_authenticate(user=buyer)
    assert reserve(client, item, variant_id=red.id).status_code == 201

    client.force_authenticate(user=owner)
    response = client.patch(f'/api/items/{item.id}/', {
        'options': json.dumps([{'name': 'اللون', 'values': ['أزرق', 'أخضر']}]),
        'variants': json.dumps([
            {'attributes': {'اللون': 'أزرق'}, 'quantity': 7, 'price': None},
            {'attributes': {'اللون': 'أخضر'}, 'quantity': 2},
        ]),
    }, format='multipart')
    assert response.status_code == 200, response.data
    item.refresh_from_db()
    assert item.options == [{'name': 'اللون', 'values': ['أزرق', 'أخضر']}]
    # Blue was edited in place (same row, price override cleared)…
    blue.refresh_from_db()
    assert blue.quantity == 7 and blue.price is None
    # …green is new, and red — reserved, so PROTECTed — is kept but retired.
    assert item.variants.filter(attributes={'اللون': 'أخضر'}, quantity=2).exists()
    red.refresh_from_db()
    assert red.is_active is False and red.quantity == 0
    assert item.quantity == 9


def test_removing_an_unreferenced_variant_deletes_it(client):
    owner = make_user('upd3')
    item, red, blue = make_optioned_item(owner)
    client.force_authenticate(user=owner)
    response = client.patch(f'/api/items/{item.id}/', {
        'options': [{'name': 'اللون', 'values': ['أزرق']}],
        'variants': [{'attributes': {'اللون': 'أزرق'}, 'quantity': 5}],
    }, format='json')
    assert response.status_code == 200, response.data
    assert not ItemVariant.objects.filter(id=red.id).exists()
    item.refresh_from_db()
    assert item.quantity == 5


def test_turning_options_off_collapses_to_one_default_variant(client):
    owner = make_user('upd4')
    item, red, blue = make_optioned_item(owner)
    client.force_authenticate(user=owner)
    response = client.patch(f'/api/items/{item.id}/', {
        'options': [], 'variants': [], 'quantity': 4,
    }, format='json')
    assert response.status_code == 200, response.data
    item.refresh_from_db()
    assert item.options == []
    assert item.variants.count() == 1
    assert item.default_variant.attributes == {}
    assert item.quantity == 4


def test_variants_without_options_are_rejected_on_edit(client):
    owner = make_user('upd5')
    item, red, blue = make_optioned_item(owner)
    client.force_authenticate(user=owner)
    response = client.patch(f'/api/items/{item.id}/', {
        'variants': [{'attributes': {'اللون': 'أزرق'}, 'quantity': 5}],
    }, format='json')
    assert response.status_code == 400
    blue.refresh_from_db()
    assert blue.quantity == 5


def test_bare_quantity_is_ignored_for_an_optioned_item(client):
    owner = make_user('upd6')
    item, red, blue = make_optioned_item(owner)
    client.force_authenticate(user=owner)
    response = client.patch(f'/api/items/{item.id}/', {'quantity': 99}, format='json')
    assert response.status_code == 200, response.data
    item.refresh_from_db()
    assert item.quantity == 6
