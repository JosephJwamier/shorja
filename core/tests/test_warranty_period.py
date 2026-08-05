"""
Per-product warranty period — regression tests.

The payment-release timer used to be a hardcoded 7 days for every product.
It is now Item.warranty_days (seller-configurable, minimum 1 day), snapshotted
onto Reservation.warranty_days at completion time so a later edit to the item
doesn't retroactively change the terms of an in-flight order. Nothing
previously pinned the old 7-day behavior in a test, so these both prove the
new per-item logic and lock in regression coverage that never existed before.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Category, Dispute, Item, Reservation, User, get_or_create_wallet
from core.wallet_utils import complete_reservation
from core import tasks as core_tasks

pytestmark = pytest.mark.django_db


def make_user(username, balance=Decimal('0.00')):
    user = User.objects.create_user(username=username, password='pass12345')
    if balance:
        get_or_create_wallet(user).credit(balance)
    return user


def make_item(owner, warranty_days=None, quantity=3):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    kwargs = dict(
        owner=owner, title='item', description='d', category=category,
        condition='good', quantity=quantity, status='active',
    )
    if warranty_days is not None:
        kwargs['warranty_days'] = warranty_days
    return Item.objects.create(**kwargs)


def make_reservation(*, item, buyer, total_price=Decimal('100.00'), quantity=1,
                      completed=True, warranty_days=None, days_ago=0, status='completed'):
    kwargs = dict(
        item=item, buyer=buyer, quantity=quantity,
        deposit_amount=total_price, total_price=total_price,
        status=status, expires_at=timezone.now() + timedelta(days=1),
        is_deposit_only=False,
    )
    if completed:
        kwargs['completed_at'] = timezone.now() - timedelta(days=days_ago)
    if warranty_days is not None:
        kwargs['warranty_days'] = warranty_days
    return Reservation.objects.create(**kwargs)


def test_item_default_warranty_days_is_seven():
    owner = make_user('owner_default')
    item = make_item(owner)
    assert item.warranty_days == 7


def test_reservation_default_warranty_days_is_seven():
    owner, buyer = make_user('owner_res_default'), make_user('buyer_res_default')
    item = make_item(owner)
    reservation = make_reservation(item=item, buyer=buyer, days_ago=2)
    assert reservation.warranty_days == 7


def test_can_release_payment_true_when_short_warranty_elapsed():
    owner, buyer = make_user('owner1'), make_user('buyer1')
    item = make_item(owner, warranty_days=1)
    reservation = make_reservation(item=item, buyer=buyer, warranty_days=1, days_ago=2)
    assert reservation.can_release_payment() is True


def test_can_release_payment_false_when_long_warranty_not_elapsed():
    owner, buyer = make_user('owner2'), make_user('buyer2')
    item = make_item(owner, warranty_days=7)
    reservation = make_reservation(item=item, buyer=buyer, warranty_days=7, days_ago=2)
    assert reservation.can_release_payment() is False


def test_can_release_payment_blocked_by_open_dispute_regardless_of_warranty():
    owner, buyer = make_user('owner3'), make_user('buyer3')
    item = make_item(owner, warranty_days=1)
    reservation = make_reservation(item=item, buyer=buyer, warranty_days=1, days_ago=5)
    Dispute.objects.create(
        item=item, reservation=reservation, complainant=buyer, defendant=owner,
        title='t', description='d', status='open',
    )
    assert reservation.can_release_payment() is False


def test_complete_reservation_snapshots_item_warranty_days():
    owner, buyer = make_user('owner4'), make_user('buyer4', balance=Decimal('1000.00'))
    item = make_item(owner, warranty_days=3)
    reservation = Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        deposit_amount=Decimal('100.00'), total_price=Decimal('100.00'),
        expires_at=timezone.now() + timedelta(days=1),
    )
    complete_reservation(reservation, payment_type='full')
    reservation.refresh_from_db()
    assert reservation.warranty_days == 3


def test_complete_reservation_snapshot_survives_later_item_edit():
    owner, buyer = make_user('owner5'), make_user('buyer5', balance=Decimal('1000.00'))
    item = make_item(owner, warranty_days=7)
    reservation = Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        deposit_amount=Decimal('100.00'), total_price=Decimal('100.00'),
        expires_at=timezone.now() + timedelta(days=1),
    )
    complete_reservation(reservation, payment_type='full')

    item.warranty_days = 1
    item.save(update_fields=['warranty_days'])

    reservation.refresh_from_db()
    assert reservation.warranty_days == 7


def test_release_payments_after_week_respects_per_item_warranty():
    owner = make_user('owner6')
    buyer1, buyer3, buyer7 = (
        make_user('buyer6_1'), make_user('buyer6_3'), make_user('buyer6_7')
    )

    item1 = make_item(owner, warranty_days=1)
    item3 = make_item(owner, warranty_days=3)
    item7 = make_item(owner, warranty_days=7)

    reservation1 = make_reservation(item=item1, buyer=buyer1, warranty_days=1, days_ago=2)
    reservation3 = make_reservation(item=item3, buyer=buyer3, warranty_days=3, days_ago=2)
    reservation7 = make_reservation(item=item7, buyer=buyer7, warranty_days=7, days_ago=2)

    core_tasks.release_payments_after_week()

    reservation1.refresh_from_db()
    reservation3.refresh_from_db()
    reservation7.refresh_from_db()

    assert reservation1.payment_released_to_seller is True
    assert reservation3.payment_released_to_seller is False
    assert reservation7.payment_released_to_seller is False


def test_get_payment_status_days_remaining_uses_item_warranty():
    from core.serializers import ReservationSerializer

    owner, buyer = make_user('owner8'), make_user('buyer8')
    item = make_item(owner, warranty_days=3)
    reservation = make_reservation(item=item, buyer=buyer, warranty_days=3, days_ago=1)

    payment_status = ReservationSerializer(reservation).get_payment_status(reservation)
    assert payment_status['days_remaining'] == 2


def test_create_item_rejects_non_positive_warranty_days():
    owner = make_user('owner9')
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    client = APIClient()
    client.force_authenticate(user=owner)

    response = client.post('/api/items/', {
        'title': 'x', 'description': 'd', 'category': category.id,
        'condition': 'good', 'quantity': 1, 'warranty_days': 0,
    })
    assert response.status_code == 400
    assert 'warranty_days' in response.data['errors']
