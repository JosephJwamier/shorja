"""The money-flow timeline (GET /wallet/activity/) and the manual re-sync of
unfinished card top-ups (POST /payments/sync-pending/).

Every entry must say which way the money went and between what — that is
the whole point of the screen — so the assertions are on direction and
labels, not just amounts.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, Location, PaymentIntent, Reservation, Transaction,
    User, WithdrawalRequest, get_or_create_wallet,
)
from core.services.payments.base import PaymentStatus, ProviderError
from core.services.payments.registry import get_provider, reset_cache
from core.services.wallet_activity import build_activity
from core.wallet_utils import complete_reservation, release_reservation_payment

pytestmark = pytest.mark.django_db

PROVIDER_SETTINGS = {
    'alqaseh': {
        'BASE_URL': 'https://api-test.example/v1',
        'CHECKOUT_BASE_URL': 'https://pay-test.example',
        'CLIENT_ID': 'test_id',
        'CLIENT_SECRET': 'test_secret',
    }
}


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture(autouse=True)
def configured(settings):
    settings.PAYMENT_PROVIDERS = PROVIDER_SETTINGS
    reset_cache()
    yield
    reset_cache()


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_item(owner, price='100.00'):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(owner=owner, title='lamp', description='d', category=category,
                               condition='new', quantity=5, status='active')
    FixedPriceListing.objects.create(item=item, price=Decimal(price), deposit_percentage=10)
    return item


def make_reservation(item, buyer):
    location = Location.objects.create(
        user=buyer, name='home', address_line1='x', city='Baghdad', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1')
    return Reservation.objects.create(
        item=item, variant=item.default_variant, buyer=buyer, quantity=1,
        deposit_amount=Decimal('10.00'), total_price=Decimal('100.00'),
        status='pending', expires_at=timezone.now() + timedelta(days=1),
        delivery_location=location,
    )


def entries_of(user, **kw):
    return build_activity(user, **kw)['entries']


def only(entries, kind):
    found = [e for e in entries if e['kind'] == kind]
    assert len(found) == 1, found
    return found[0]


# ---------------------------------------------------------------------------
# Timeline entries
# ---------------------------------------------------------------------------

def test_topup_is_money_in_from_the_card():
    user = make_user('act_topup')
    Transaction.objects.create(user=user, transaction_type='payment',
                               amount=Decimal('70000'), status='completed')
    e = only(entries_of(user), 'topup')
    assert e['direction'] == 'in'
    assert e['from'] == 'بطاقة (القاصة)' and e['to'] == 'المحفظة'
    assert e['amount'] == '70000.00'


def test_purchase_is_held_until_released_then_out_and_the_seller_sees_it_in():
    seller, buyer = make_user('act_seller'), make_user('act_buyer')
    get_or_create_wallet(buyer).credit(Decimal('1000'))
    item = make_item(seller)
    reservation = make_reservation(item, buyer)
    complete_reservation(reservation, 'full')

    paid = only(entries_of(buyer), 'purchase')
    assert paid['direction'] == 'hold'
    assert paid['from'] == 'المحفظة' and paid['to'].startswith('حجز #%d' % reservation.id)
    assert 'الضمان' in paid['note']
    # Nothing has reached the seller yet — it shows as earnings in escrow.
    assert build_activity(seller)['escrow']['earnings_waiting'] == '100.00'
    assert build_activity(buyer)['escrow']['paid_waiting'] == '100.00'

    release_reservation_payment(reservation)
    reservation.refresh_from_db()

    paid = only(entries_of(buyer), 'purchase')
    assert paid['direction'] == 'out' and paid['note'] == 'سُلّم للبائع'
    sale = only(entries_of(seller), 'sale')
    assert sale['direction'] == 'in'
    assert sale['from'].startswith('حجز #%d' % reservation.id) and sale['to'] == 'المحفظة'
    assert build_activity(seller)['escrow']['earnings_waiting'] == '0.00'


def test_pending_withdrawal_is_a_hold_and_a_rejected_one_a_release():
    user = make_user('act_wd')
    wallet = get_or_create_wallet(user)
    wallet.credit(Decimal('50000'))
    wallet.hold(Decimal('20000'))
    w = WithdrawalRequest.objects.create(user=user, amount=Decimal('20000'), card_last4='1234')

    data = build_activity(user)
    assert data['held_balance'] == '20000.00' and data['available_balance'] == '30000.00'
    e = only(data['entries'], 'withdrawal_request')
    assert e['direction'] == 'hold' and e['to'] == 'بطاقة ****1234'

    w.status = 'rejected'; w.save()
    e = only(entries_of(user), 'withdrawal_request')
    assert e['direction'] == 'release'


def test_approved_withdrawal_appears_once_as_the_payout_row():
    user = make_user('act_wd2')
    WithdrawalRequest.objects.create(user=user, amount=Decimal('5000'), status='approved')
    Transaction.objects.create(user=user, transaction_type='payout',
                               amount=Decimal('5000'), status='completed')
    entries = entries_of(user)
    assert [e['kind'] for e in entries] == ['withdrawal']
    assert entries[0]['direction'] == 'out'


def test_unfinished_and_failed_topups_are_shown_but_never_counted():
    user = make_user('act_pi')
    PaymentIntent.objects.create(user=user, provider='alqaseh', amount=Decimal('80000'),
                                 currency='IQD', reference='PI-open', provider_payment_id='p1')
    PaymentIntent.objects.create(user=user, provider='alqaseh', amount=Decimal('9000'),
                                 currency='IQD', reference='PI-dead', provider_payment_id='p2',
                                 state=PaymentIntent.STATE_FAILED)
    data = build_activity(user)
    assert data['pending_topups'] == 1
    by_ref = {e['ref_id']: e for e in data['entries']}
    assert by_ref['PI-open']['direction'] == 'pending'
    assert by_ref['PI-dead']['direction'] == 'failed'
    assert data['balance'] == '0.00'


def test_activity_endpoint_is_per_user(client):
    me, other = make_user('act_me'), make_user('act_other')
    Transaction.objects.create(user=other, transaction_type='payment',
                               amount=Decimal('1'), status='completed')
    client.force_authenticate(user=me)
    response = client.get('/api/wallet/activity/')
    assert response.status_code == 200
    assert response.data['data']['entries'] == []


# ---------------------------------------------------------------------------
# Manual re-sync
# ---------------------------------------------------------------------------

def make_intent(user, reference, **extra):
    defaults = dict(user=user, provider='alqaseh', amount=Decimal('50000'), currency='IQD',
                    reference=reference, provider_payment_id='pay_' + reference)
    defaults.update(extra)
    return PaymentIntent.objects.create(**defaults)


def test_sync_pending_settles_what_the_gateway_now_reports_paid(client):
    user = make_user('sync1')
    make_intent(user, 'PI-a')
    make_intent(user, 'PI-b')
    provider = get_provider('alqaseh')

    def fake_status(*, intent):
        state = 'succeeded' if intent.reference == 'PI-a' else 'pending'
        return PaymentStatus(state=state, raw={'payment_status': state})

    client.force_authenticate(user=user)
    with patch.object(type(provider), 'fetch_status', side_effect=fake_status):
        response = client.post('/api/payments/sync-pending/')

    assert response.status_code == 200, response.data
    assert response.data['data'] == {
        'checked': 2, 'settled': 1, 'still_pending': 1, 'failed': 0, 'errors': 0}
    assert get_or_create_wallet(user).balance == Decimal('50000')
    assert PaymentIntent.objects.get(reference='PI-b').state == PaymentIntent.STATE_PENDING


def test_sync_pending_skips_final_old_and_other_users_intents(client):
    user, other = make_user('sync2'), make_user('sync2o')
    make_intent(user, 'PI-done', state=PaymentIntent.STATE_SUCCEEDED,
                applied_at=timezone.now())
    stale = make_intent(user, 'PI-stale')
    PaymentIntent.objects.filter(pk=stale.pk).update(
        created_at=timezone.now() - timedelta(days=3))
    make_intent(other, 'PI-theirs')
    provider = get_provider('alqaseh')

    client.force_authenticate(user=user)
    with patch.object(type(provider), 'fetch_status') as fetch:
        response = client.post('/api/payments/sync-pending/')

    assert response.status_code == 200
    assert response.data['data']['checked'] == 0
    fetch.assert_not_called()


def test_sync_pending_treats_a_gateway_outage_as_still_pending(client):
    user = make_user('sync3')
    make_intent(user, 'PI-out')
    provider = get_provider('alqaseh')

    client.force_authenticate(user=user)
    with patch.object(type(provider), 'fetch_status',
                      side_effect=ProviderError('gateway down')):
        response = client.post('/api/payments/sync-pending/')

    assert response.status_code == 200
    assert response.data['data']['errors'] == 1
    assert PaymentIntent.objects.get(reference='PI-out').state == PaymentIntent.STATE_PENDING
    assert get_or_create_wallet(user).balance == Decimal('0')


def test_sync_pending_requires_login(client):
    assert client.post('/api/payments/sync-pending/').status_code in (401, 403)
