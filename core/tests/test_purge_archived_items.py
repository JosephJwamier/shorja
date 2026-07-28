"""
core.tasks.purge_archived_items — the scheduled hard-delete that runs long
after archive_item()/ItemViewSet.destroy soft-delete an Item. Must only ever
touch items that are both archived long enough ago and fully settled (no
open reservation/dispute/in-flight delivery order).
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from core.models import Category, DeliveryOrder, Dispute, Item, Reservation, User
from core.tasks import purge_archived_items

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_item(owner, item_status='archived', age_days=400):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(
        owner=owner, title='item', description='d', category=category,
        condition='good', quantity=1, status=item_status,
    )
    backdated = timezone.now() - timedelta(days=age_days)
    Item.objects.filter(id=item.id).update(updated_at=backdated)
    item.refresh_from_db()
    return item


def make_reservation(item, buyer, res_status='cancelled'):
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10.00'), total_price=Decimal('100.00'),
        status=res_status, expires_at=timezone.now() + timedelta(hours=1),
    )


def test_old_archived_item_with_no_history_is_purged():
    owner = make_user('purge_owner1')
    item = make_item(owner, age_days=400)

    purge_archived_items()

    assert not Item.objects.filter(id=item.id).exists()


def test_recently_archived_item_is_kept():
    owner = make_user('purge_owner2')
    item = make_item(owner, age_days=10)

    purge_archived_items()

    assert Item.objects.filter(id=item.id).exists()


def test_active_item_is_never_purged_even_if_old():
    owner = make_user('purge_owner3')
    item = make_item(owner, item_status='active', age_days=400)

    purge_archived_items()

    assert Item.objects.filter(id=item.id).exists()


def test_item_with_open_reservation_is_kept():
    owner = make_user('purge_owner4')
    buyer = make_user('purge_buyer4')
    item = make_item(owner, age_days=400)
    make_reservation(item, buyer, res_status='completed')  # non-terminal

    purge_archived_items()

    assert Item.objects.filter(id=item.id).exists()


def test_item_with_terminal_reservation_only_is_purged():
    owner = make_user('purge_owner5')
    buyer = make_user('purge_buyer5')
    item = make_item(owner, age_days=400)
    make_reservation(item, buyer, res_status='cancelled')  # terminal

    purge_archived_items()

    assert not Item.objects.filter(id=item.id).exists()


def test_item_with_open_dispute_is_kept():
    owner = make_user('purge_owner6')
    buyer = make_user('purge_buyer6')
    item = make_item(owner, age_days=400)
    reservation = make_reservation(item, buyer, res_status='cancelled')
    Dispute.objects.create(
        item=item, reservation=reservation, complainant=buyer, defendant=owner,
        title='t', description='d', status='open',
    )

    purge_archived_items()

    assert Item.objects.filter(id=item.id).exists()


def test_item_with_in_flight_delivery_order_is_kept():
    owner = make_user('purge_owner7')
    buyer = make_user('purge_buyer7')
    item = make_item(owner, age_days=400)
    reservation = make_reservation(item, buyer, res_status='cancelled')
    DeliveryOrder.objects.create(
        reservation=reservation, direction='outbound', status='picked_up',
        from_name='n', from_phone='1', from_address='addr',
        to_name='n', to_phone='1', to_address='addr',
        item_summary='item',
    )

    purge_archived_items()

    assert Item.objects.filter(id=item.id).exists()
