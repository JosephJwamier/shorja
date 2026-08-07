"""
Reservation.can_approve gating — regression tests.

get_can_approve() (ReservationSerializer) decides whether the buyer's "تم
التأكد من المنتج" button is shown — pressing it triggers
approve_and_release_payment, an early/manual payment release. It must not
surface before the buyer has actually confirmed physical receipt of the
item (DeliveryOrderViewSet.confirm_receipt moving the outbound
DeliveryOrder to 'confirmed'): a reservation that's merely been paid for,
with delivery not even started, previously fell through to a plain
`status == 'completed'` check and offered the release button immediately —
before the buyer had anything in hand.

The one exception is when there's no active delivery company at all (the
old direct hand-off flow, where paying is the only fulfilment step) — see
get_can_request_payment_release for the same distinction.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from core.models import Category, DeliveryCompany, DeliveryOrder, Item, Reservation, User, get_or_create_wallet
from core.serializers import ReservationSerializer
from core.wallet_utils import complete_reservation

pytestmark = pytest.mark.django_db


class _FakeRequest:
    def __init__(self, user):
        self.user = user


def make_user(username, balance=Decimal('1000.00')):
    user = User.objects.create_user(username=username, password='pass12345')
    if balance:
        get_or_create_wallet(user).credit(balance)
    return user


def make_item(owner):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return Item.objects.create(
        owner=owner, title='item', description='d', category=category,
        condition='good', quantity=3, status='active',
    )


def make_paid_reservation(owner, buyer):
    item = make_item(owner)
    reservation = Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        deposit_amount=Decimal('100.00'), total_price=Decimal('100.00'),
        expires_at=timezone.now() + timedelta(days=1),
    )
    complete_reservation(reservation, payment_type='full')
    reservation.refresh_from_db()
    return reservation


def make_delivery_order(reservation, *, status='assigned'):
    return DeliveryOrder.objects.create(
        reservation=reservation, direction='outbound', status=status,
        from_name='seller', from_phone='0000', from_address='a',
        to_name='buyer', to_phone='1111', to_address='b',
        item_summary='item',
    )


def can_approve(reservation, user):
    return ReservationSerializer(
        reservation, context={'request': _FakeRequest(user)}
    ).get_can_approve(reservation)


def test_cannot_approve_right_after_payment_when_delivery_company_active():
    owner, buyer = make_user('owner1'), make_user('buyer1')
    DeliveryCompany.objects.create(name='fast co')
    reservation = make_paid_reservation(owner, buyer)
    assert can_approve(reservation, buyer) is False


def test_cannot_approve_while_delivery_still_in_transit():
    owner, buyer = make_user('owner2'), make_user('buyer2')
    DeliveryCompany.objects.create(name='fast co')
    reservation = make_paid_reservation(owner, buyer)
    make_delivery_order(reservation, status='picked_up')
    assert can_approve(reservation, buyer) is False


def test_cannot_approve_after_delivered_but_not_yet_confirmed():
    owner, buyer = make_user('owner3'), make_user('buyer3')
    DeliveryCompany.objects.create(name='fast co')
    reservation = make_paid_reservation(owner, buyer)
    make_delivery_order(reservation, status='delivered')
    assert can_approve(reservation, buyer) is False


def test_can_approve_once_buyer_confirmed_delivery():
    owner, buyer = make_user('owner4'), make_user('buyer4')
    DeliveryCompany.objects.create(name='fast co')
    reservation = make_paid_reservation(owner, buyer)
    make_delivery_order(reservation, status='confirmed')
    assert can_approve(reservation, buyer) is True


def test_can_approve_right_after_payment_when_no_delivery_company_at_all():
    owner, buyer = make_user('owner5'), make_user('buyer5')
    reservation = make_paid_reservation(owner, buyer)
    assert can_approve(reservation, buyer) is True


def test_seller_cannot_approve_even_when_eligible():
    owner, buyer = make_user('owner6'), make_user('buyer6')
    reservation = make_paid_reservation(owner, buyer)
    assert can_approve(reservation, owner) is False
