"""
Phase 3 — resolve_dispute() regression tests.

This function moves real money (core/services/disputes.py), extracted
verbatim out of DisputeViewSet.resolve so the admin console and the DRF API
share one implementation. These tests exist to prove the extraction didn't
change behavior, and to lock in the two properties that matter most for
money-moving code: a rejected action writes nothing, and a mid-transfer
failure (insufficient balance) rolls back everything, including the
dispute's own status field.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Category, Dispute, Item, Reservation, User, get_or_create_wallet
from core.services.disputes import DisputeResolutionError, resolve_dispute

pytestmark = pytest.mark.django_db


def make_user(username, balance=Decimal('0.00')):
    user = User.objects.create_user(username=username, password='pass12345')
    if balance:
        get_or_create_wallet(user).credit(balance)
    return user


def wallet_balance(user):
    return get_or_create_wallet(user).balance


def make_reservation(*, owner, buyer, total_price=Decimal('100.00'), quantity=2,
                      item_quantity=3, completed=True, payment_released=False,
                      status='disputed'):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(
        owner=owner, title='item', description='d', category=category,
        condition='used', quantity=item_quantity, status='reserved',
    )
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=quantity,
        deposit_amount=Decimal('10.00'), total_price=total_price,
        status=status, expires_at=timezone.now() + timedelta(days=1),
        completed_at=timezone.now() if completed else None,
        is_deposit_only=False,
        payment_released_to_seller=payment_released,
    )


def make_dispute(reservation, complainant=None, defendant=None):
    return Dispute.objects.create(
        item=reservation.item, reservation=reservation,
        complainant=complainant or reservation.buyer,
        defendant=defendant or reservation.item.owner,
        title='t', description='d',
    )


# ---------------------------------------------------------------------------
# refund_buyer
# ---------------------------------------------------------------------------

def test_refund_buyer_before_payout_restores_stock_and_credits_buyer():
    owner, buyer = make_user('owner1'), make_user('buyer1')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('100'),
                                    quantity=2, item_quantity=3, payment_released=False)
    dispute = make_dispute(reservation)

    resolve_dispute(dispute, resolution_action='refund_buyer', resolution='ok', admin_notes='n')

    dispute.refresh_from_db()
    reservation.refresh_from_db()
    reservation.item.refresh_from_db()

    assert dispute.status == 'resolved'
    assert dispute.resolution_action == 'refund_buyer'
    assert dispute.resolved_at is not None
    assert reservation.status == 'refunded'
    assert reservation.item.quantity == 5  # 3 + the 2 restored
    assert reservation.item.status == 'active'
    assert wallet_balance(buyer) == Decimal('100.00')
    assert wallet_balance(owner) == Decimal('0.00')  # never held the money


def test_refund_buyer_after_payout_claws_back_from_seller():
    owner, buyer = make_user('owner2', balance=Decimal('200')), make_user('buyer2')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('100'),
                                    payment_released=True)
    dispute = make_dispute(reservation)

    resolve_dispute(dispute, resolution_action='refund_buyer')

    assert wallet_balance(owner) == Decimal('100.00')
    assert wallet_balance(buyer) == Decimal('100.00')
    reservation.refresh_from_db()
    assert reservation.status == 'refunded'


def test_refund_buyer_insufficient_seller_balance_rolls_back_everything():
    owner, buyer = make_user('owner3', balance=Decimal('0')), make_user('buyer3')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('100'),
                                    payment_released=True, status='disputed')
    dispute = make_dispute(reservation)

    with pytest.raises(ValueError):
        resolve_dispute(dispute, resolution_action='refund_buyer')

    dispute.refresh_from_db()
    reservation.refresh_from_db()
    assert dispute.status == 'open'  # not left half-resolved
    assert reservation.status == 'disputed'  # unchanged
    assert wallet_balance(buyer) == Decimal('0.00')  # credit never ran


# ---------------------------------------------------------------------------
# pay_seller
# ---------------------------------------------------------------------------

def test_pay_seller_requires_completed_reservation():
    owner, buyer = make_user('owner4'), make_user('buyer4')
    reservation = make_reservation(owner=owner, buyer=buyer, completed=False)
    dispute = make_dispute(reservation)

    with pytest.raises(DisputeResolutionError):
        resolve_dispute(dispute, resolution_action='pay_seller')

    dispute.refresh_from_db()
    assert dispute.status == 'open'
    assert wallet_balance(owner) == Decimal('0.00')


def test_pay_seller_releases_payment_and_confirms_reservation():
    owner, buyer = make_user('owner5'), make_user('buyer5')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('100'),
                                    completed=True, payment_released=False)
    dispute = make_dispute(reservation)

    resolve_dispute(dispute, resolution_action='pay_seller')

    assert wallet_balance(owner) == Decimal('100.00')
    reservation.refresh_from_db()
    assert reservation.status == 'confirmed'
    assert reservation.payment_released_to_seller is True


def test_no_action_pays_seller_same_as_pay_seller():
    owner, buyer = make_user('owner6'), make_user('buyer6')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('75'),
                                    completed=True, payment_released=False)
    dispute = make_dispute(reservation)

    resolve_dispute(dispute, resolution_action='no_action')

    assert wallet_balance(owner) == Decimal('75.00')
    reservation.refresh_from_db()
    assert reservation.status == 'confirmed'


# ---------------------------------------------------------------------------
# partial_refund
# ---------------------------------------------------------------------------

def test_partial_refund_exceeding_amount_charged_is_rejected():
    owner, buyer = make_user('owner7'), make_user('buyer7')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('100'))
    dispute = make_dispute(reservation)

    with pytest.raises(DisputeResolutionError):
        resolve_dispute(dispute, resolution_action='partial_refund', partial_refund_amount=Decimal('150'))

    dispute.refresh_from_db()
    assert dispute.status == 'open'


def test_partial_refund_before_payout_splits_between_buyer_and_seller():
    owner, buyer = make_user('owner8'), make_user('buyer8')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('100'),
                                    payment_released=False)
    dispute = make_dispute(reservation)

    resolve_dispute(dispute, resolution_action='partial_refund', partial_refund_amount=Decimal('40'))

    assert wallet_balance(buyer) == Decimal('40.00')
    assert wallet_balance(owner) == Decimal('60.00')
    reservation.refresh_from_db()
    assert reservation.status == 'refunded'


def test_partial_refund_after_payout_claws_back_only_the_partial_amount():
    owner, buyer = make_user('owner9', balance=Decimal('200')), make_user('buyer9')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('100'),
                                    payment_released=True)
    dispute = make_dispute(reservation)

    resolve_dispute(dispute, resolution_action='partial_refund', partial_refund_amount=Decimal('30'))

    assert wallet_balance(owner) == Decimal('170.00')
    assert wallet_balance(buyer) == Decimal('30.00')
    reservation.refresh_from_db()
    assert reservation.status == 'refunded'


# ---------------------------------------------------------------------------
# item-only dispute (no reservation attached)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The DRF endpoint still calls resolve_dispute() correctly end-to-end —
# proves the refactor (views.py now delegates instead of doing the work
# inline) didn't change the API contract.
# ---------------------------------------------------------------------------

def test_drf_resolve_endpoint_moves_money_and_returns_original_response_shape():
    owner, buyer = make_user('owner11'), make_user('buyer11')
    staff = User.objects.create_user(username='staff11', password='x', is_staff=True)
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('50'),
                                    completed=True, payment_released=False)
    dispute = make_dispute(reservation)

    client = APIClient()
    client.force_authenticate(user=staff)
    response = client.post(f'/api/disputes/{dispute.id}/resolve/', {
        'resolution': 'seller wins',
        'resolution_action': 'pay_seller',
    }, format='json')

    assert response.status_code == 200
    body = response.json()
    assert body['success'] is True
    assert 'data' in body
    assert wallet_balance(owner) == Decimal('50.00')


def test_drf_resolve_endpoint_surfaces_precondition_failure_as_400():
    owner, buyer = make_user('owner12'), make_user('buyer12')
    staff = User.objects.create_user(username='staff12', password='x', is_staff=True)
    reservation = make_reservation(owner=owner, buyer=buyer, completed=False)
    dispute = make_dispute(reservation)

    client = APIClient()
    client.force_authenticate(user=staff)
    response = client.post(f'/api/disputes/{dispute.id}/resolve/', {
        'resolution': 'x',
        'resolution_action': 'pay_seller',
    }, format='json')

    assert response.status_code == 400
    assert response.json()['success'] is False
    dispute.refresh_from_db()
    assert dispute.status == 'open'


def test_drf_resolve_endpoint_rejects_non_staff():
    owner, buyer = make_user('owner13'), make_user('buyer13')
    reservation = make_reservation(owner=owner, buyer=buyer, completed=True)
    dispute = make_dispute(reservation)

    client = APIClient()
    client.force_authenticate(user=buyer)  # a party to the dispute, not staff
    response = client.post(f'/api/disputes/{dispute.id}/resolve/', {
        'resolution': 'x', 'resolution_action': 'pay_seller',
    }, format='json')

    assert response.status_code == 403


def test_item_only_dispute_resolves_without_touching_money():
    owner, buyer = make_user('owner10'), make_user('buyer10')
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(owner=owner, title='i', description='d', category=category,
                                condition='used', quantity=1, status='active')
    dispute = Dispute.objects.create(item=item, reservation=None, complainant=buyer,
                                      defendant=owner, title='t', description='d')

    resolve_dispute(dispute, resolution_action='no_action', resolution='dismissed')

    dispute.refresh_from_db()
    assert dispute.status == 'resolved'
    assert dispute.resolution_action == 'no_action'
    assert wallet_balance(owner) == Decimal('0.00')
    assert wallet_balance(buyer) == Decimal('0.00')
