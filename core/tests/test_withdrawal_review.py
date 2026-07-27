"""
Phase 5 — approve_withdrawal()/reject_withdrawal() regression tests, plus
the DRF endpoint that now delegates to them.

Mirrors test_dispute_resolution.py's shape: build a known wallet state,
call the service, check exact balances — and prove a rejected precondition
(already reviewed) writes nothing.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from core.models import User, WithdrawalRequest, get_or_create_wallet
from core.services.withdrawals import WithdrawalReviewError, approve_withdrawal, reject_withdrawal

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def wallet_balance(user):
    return get_or_create_wallet(user).balance


def make_pending_withdrawal(user, amount=Decimal('50')):
    """Mirrors what WalletViewSet.withdraw actually does: hold the funds
    first, then create the pending request."""
    wallet = get_or_create_wallet(user)
    wallet.credit(amount)  # give them something to withdraw
    wallet.hold(amount)
    return WithdrawalRequest.objects.create(user=user, amount=amount, status='pending')


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

def test_approve_settles_hold_and_leaves_balance_reduced():
    user = make_user('wd_user1')
    admin = make_user('wd_admin1')
    withdrawal = make_pending_withdrawal(user, Decimal('50'))

    approve_withdrawal(withdrawal, reviewed_by=admin, admin_notes='ok')

    withdrawal.refresh_from_db()
    wallet = get_or_create_wallet(user)
    assert withdrawal.status == 'approved'
    assert withdrawal.reviewed_by_id == admin.id
    assert withdrawal.reviewed_at is not None
    assert withdrawal.admin_notes == 'ok'
    assert wallet.balance == Decimal('0.00')  # paid out
    assert wallet.held_balance == Decimal('0.00')  # hold released too


def test_approve_creates_a_payout_transaction():
    from core.models import Transaction

    user = make_user('wd_user2')
    admin = make_user('wd_admin2')
    withdrawal = make_pending_withdrawal(user, Decimal('30'))

    approve_withdrawal(withdrawal, reviewed_by=admin)

    txn = Transaction.objects.filter(user=user, transaction_type='payout').first()
    assert txn is not None
    assert txn.amount == Decimal('30')
    assert txn.status == 'completed'


def test_approve_rejects_already_reviewed_withdrawal():
    user = make_user('wd_user3')
    admin = make_user('wd_admin3')
    withdrawal = make_pending_withdrawal(user, Decimal('20'))
    withdrawal.status = 'approved'
    withdrawal.save(update_fields=['status'])

    with pytest.raises(WithdrawalReviewError):
        approve_withdrawal(withdrawal, reviewed_by=admin)

    # Balance/hold untouched by the rejected second attempt.
    wallet = get_or_create_wallet(user)
    assert wallet.held_balance == Decimal('20.00')


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------

def test_reject_releases_hold_back_to_available_balance():
    user = make_user('wd_user4')
    admin = make_user('wd_admin4')
    withdrawal = make_pending_withdrawal(user, Decimal('40'))

    reject_withdrawal(withdrawal, reviewed_by=admin, admin_notes='بيانات البطاقة غير صحيحة')

    withdrawal.refresh_from_db()
    wallet = get_or_create_wallet(user)
    assert withdrawal.status == 'rejected'
    assert withdrawal.admin_notes == 'بيانات البطاقة غير صحيحة'
    assert wallet.balance == Decimal('40.00')  # money never left
    assert wallet.held_balance == Decimal('0.00')  # no longer held
    assert wallet.available_balance == Decimal('40.00')  # spendable again


def test_reject_rejects_already_reviewed_withdrawal():
    user = make_user('wd_user5')
    admin = make_user('wd_admin5')
    withdrawal = make_pending_withdrawal(user, Decimal('15'))
    withdrawal.status = 'rejected'
    withdrawal.save(update_fields=['status'])

    with pytest.raises(WithdrawalReviewError):
        reject_withdrawal(withdrawal, reviewed_by=admin)


# ---------------------------------------------------------------------------
# DRF endpoint still calls the service end-to-end
# ---------------------------------------------------------------------------

def test_drf_approve_endpoint_moves_money():
    user = make_user('wd_user6')
    staff = User.objects.create_user(username='wd_staff6', password='x', is_staff=True)
    withdrawal = make_pending_withdrawal(user, Decimal('25'))

    client = APIClient()
    client.force_authenticate(user=staff)
    response = client.post(f'/api/admin/withdrawals/{withdrawal.id}/approve/', {}, format='json')

    assert response.status_code == 200
    body = response.json()
    assert body['success'] is True
    assert wallet_balance(user) == Decimal('0.00')


def test_drf_approve_endpoint_returns_400_for_already_reviewed():
    user = make_user('wd_user7')
    staff = User.objects.create_user(username='wd_staff7', password='x', is_staff=True)
    withdrawal = make_pending_withdrawal(user, Decimal('10'))
    withdrawal.status = 'approved'
    withdrawal.save(update_fields=['status'])

    client = APIClient()
    client.force_authenticate(user=staff)
    response = client.post(f'/api/admin/withdrawals/{withdrawal.id}/approve/', {}, format='json')

    assert response.status_code == 400
    assert response.json()['success'] is False


def test_drf_reject_endpoint_releases_funds():
    user = make_user('wd_user8')
    staff = User.objects.create_user(username='wd_staff8', password='x', is_staff=True)
    withdrawal = make_pending_withdrawal(user, Decimal('35'))

    client = APIClient()
    client.force_authenticate(user=staff)
    response = client.post(f'/api/admin/withdrawals/{withdrawal.id}/reject/', {'admin_notes': 'x'}, format='json')

    assert response.status_code == 200
    assert wallet_balance(user) == Decimal('35.00')
