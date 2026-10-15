"""
A reservation created without a delivery address (e.g. an auction winner
who never saved one — see auction_utils.create_winner_reservation) must not
be completable until one is attached: ReservationViewSet.complete blocks on
missing delivery_location before touching payment.

Also covers update_delivery_location: the buyer may change the address on
their own reservation right up until the seller hands it off to the
delivery company (an outbound DeliveryOrder exists), after which it's locked.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Category, DeliveryOrder, Item, Location, Reservation, User, get_or_create_wallet

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_location(user, city='Baghdad'):
    return Location.objects.create(
        user=user, name='home', address_line1='x', city=city, state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1',
    )


def make_reservation(*, owner, buyer, delivery_location=None, status='pending'):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(
        owner=owner, title='item', description='d', category=category,
        condition='used', quantity=3, status='reserved',
    )
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10.00'), total_price=Decimal('100.00'),
        status=status, expires_at=timezone.now() + timedelta(days=1),
        delivery_location=delivery_location,
    )


def make_outbound_delivery_order(reservation):
    return DeliveryOrder.objects.create(
        reservation=reservation, direction='outbound',
        from_name='s', from_phone='1', from_address='a',
        to_name='b', to_phone='2', to_address='b',
        item_summary='item',
    )


def auth(client, user):
    client.force_authenticate(user=user)
    return client


def test_complete_rejects_reservation_with_no_delivery_address():
    owner, buyer = make_user('owner1'), make_user('buyer1')
    get_or_create_wallet(buyer).credit(Decimal('1000.00'))
    reservation = make_reservation(owner=owner, buyer=buyer, delivery_location=None)
    client = auth(APIClient(), buyer)

    response = client.post(f'/api/reservations/{reservation.id}/complete/', {'payment_type': 'full'})

    assert response.status_code == 400
    reservation.refresh_from_db()
    assert reservation.status == 'pending'


def test_complete_attaches_delivery_location_id_passed_inline():
    owner, buyer = make_user('owner3'), make_user('buyer3')
    get_or_create_wallet(buyer).credit(Decimal('1000.00'))
    location = Location.objects.create(
        user=buyer, name='home', address_line1='x', city='Baghdad', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1',
    )
    reservation = make_reservation(owner=owner, buyer=buyer, delivery_location=None)
    client = auth(APIClient(), buyer)

    response = client.post(f'/api/reservations/{reservation.id}/complete/', {
        'payment_type': 'full', 'delivery_location_id': location.id,
    })

    assert response.status_code == 200, response.data
    reservation.refresh_from_db()
    assert reservation.status == 'completed'
    assert reservation.delivery_location_id == location.id


def test_complete_rejects_delivery_location_id_belonging_to_another_user():
    owner, buyer = make_user('owner4'), make_user('buyer4')
    stranger = make_user('stranger4')
    get_or_create_wallet(buyer).credit(Decimal('1000.00'))
    other_location = Location.objects.create(
        user=stranger, name='home', address_line1='x', city='Baghdad', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1',
    )
    reservation = make_reservation(owner=owner, buyer=buyer, delivery_location=None)
    client = auth(APIClient(), buyer)

    response = client.post(f'/api/reservations/{reservation.id}/complete/', {
        'payment_type': 'full', 'delivery_location_id': other_location.id,
    })

    assert response.status_code == 400
    reservation.refresh_from_db()
    assert reservation.status == 'pending'
    assert reservation.delivery_location_id is None


def test_complete_succeeds_once_delivery_address_is_attached():
    owner, buyer = make_user('owner2'), make_user('buyer2')
    get_or_create_wallet(buyer).credit(Decimal('1000.00'))
    location = Location.objects.create(
        user=buyer, name='home', address_line1='x', city='Baghdad', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1',
    )
    reservation = make_reservation(owner=owner, buyer=buyer, delivery_location=location)
    client = auth(APIClient(), buyer)

    response = client.post(f'/api/reservations/{reservation.id}/complete/', {'payment_type': 'full'})

    assert response.status_code == 200, response.data
    reservation.refresh_from_db()
    assert reservation.status == 'completed'


def test_buyer_can_update_delivery_location_before_sent_to_delivery():
    owner, buyer = make_user('owner5'), make_user('buyer5')
    old_location = make_location(buyer, city='Baghdad')
    new_location = make_location(buyer, city='Basra')
    reservation = make_reservation(
        owner=owner, buyer=buyer, delivery_location=old_location, status='completed',
    )
    client = auth(APIClient(), buyer)

    response = client.post(
        f'/api/reservations/{reservation.id}/update-delivery-location/',
        {'delivery_location_id': new_location.id},
    )

    assert response.status_code == 200, response.data
    reservation.refresh_from_db()
    assert reservation.delivery_location_id == new_location.id


def test_update_delivery_location_rejects_once_sent_to_delivery():
    owner, buyer = make_user('owner6'), make_user('buyer6')
    old_location = make_location(buyer, city='Baghdad')
    new_location = make_location(buyer, city='Basra')
    reservation = make_reservation(
        owner=owner, buyer=buyer, delivery_location=old_location, status='completed',
    )
    make_outbound_delivery_order(reservation)
    client = auth(APIClient(), buyer)

    response = client.post(
        f'/api/reservations/{reservation.id}/update-delivery-location/',
        {'delivery_location_id': new_location.id},
    )

    assert response.status_code == 400
    reservation.refresh_from_db()
    assert reservation.delivery_location_id == old_location.id


def test_update_delivery_location_rejects_non_buyer():
    owner, buyer = make_user('owner7'), make_user('buyer7')
    old_location = make_location(buyer, city='Baghdad')
    new_location = make_location(buyer, city='Basra')
    reservation = make_reservation(
        owner=owner, buyer=buyer, delivery_location=old_location, status='completed',
    )
    client = auth(APIClient(), owner)

    response = client.post(
        f'/api/reservations/{reservation.id}/update-delivery-location/',
        {'delivery_location_id': new_location.id},
    )

    assert response.status_code == 403
    reservation.refresh_from_db()
    assert reservation.delivery_location_id == old_location.id


def test_update_delivery_location_rejects_location_belonging_to_another_user():
    owner, buyer = make_user('owner8'), make_user('buyer8')
    stranger = make_user('stranger8')
    old_location = make_location(buyer, city='Baghdad')
    other_location = make_location(stranger, city='Basra')
    reservation = make_reservation(
        owner=owner, buyer=buyer, delivery_location=old_location, status='completed',
    )
    client = auth(APIClient(), buyer)

    response = client.post(
        f'/api/reservations/{reservation.id}/update-delivery-location/',
        {'delivery_location_id': other_location.id},
    )

    assert response.status_code == 400
    reservation.refresh_from_db()
    assert reservation.delivery_location_id == old_location.id
