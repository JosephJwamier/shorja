"""Payment gateway integration.

Every gateway call is mocked at the provider boundary — no network here.

The cases that matter are not the happy path. They are: a webhook that lies, a
webhook and a poll racing to settle the same payment, and a gateway status this
code has never seen. Getting any of those wrong either credits a wallet for
money nobody paid, or credits it twice.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from core.models import DepositRequest, PaymentIntent, User, get_or_create_wallet
from core.services.payments.base import CreatedPayment, PaymentStatus, ProviderError
from core.services.payments.registry import get_provider, reset_cache
from core.services.payments.service import sync_intent

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


def make_intent(user, **extra):
    defaults = dict(
        user=user, provider='alqaseh', amount=Decimal('50000'), currency='IQD',
        reference='PI-test-%s' % user.username, provider_payment_id='pay_1',
    )
    defaults.update(extra)
    return PaymentIntent.objects.create(**defaults)


def status_of(state, **raw):
    return PaymentStatus(state=state, raw=raw or {'payment_status': state})


# ---------------------------------------------------------------------------
# Creating a payment
# ---------------------------------------------------------------------------

def test_create_returns_a_checkout_url(client):
    user = make_user('pay_create')
    client.force_authenticate(user=user)

    created = CreatedPayment(provider_payment_id='pay_9', provider_token='tok_9',
                             checkout_url='https://pay-test.example/pay/tok_9')
    with patch.object(type(get_provider('alqaseh')), 'create_payment', return_value=created):
        response = client.post('/api/payments/create/',
                               {'provider': 'alqaseh', 'amount': '50000'}, format='json')

    assert response.status_code == 201
    assert response.data['data']['checkout_url'] == 'https://pay-test.example/pay/tok_9'
    intent = PaymentIntent.objects.get(reference=response.data['data']['reference'])
    assert intent.state == PaymentIntent.STATE_PENDING
    assert intent.applied_at is None


def test_gateway_failure_leaves_a_row_behind(client):
    """The row is written before the gateway call, so a failed create is still
    reconcilable — the gateway may have registered it before erroring."""
    user = make_user('pay_gwfail')
    client.force_authenticate(user=user)

    with patch.object(type(get_provider('alqaseh')), 'create_payment',
                      side_effect=ProviderError('boom')):
        response = client.post('/api/payments/create/',
                               {'provider': 'alqaseh', 'amount': '50000'}, format='json')

    assert response.status_code == 502
    intent = PaymentIntent.objects.get(user=user)
    assert intent.state == PaymentIntent.STATE_FAILED


def test_amount_below_the_floor_is_rejected(client):
    user = make_user('pay_small')
    client.force_authenticate(user=user)
    response = client.post('/api/payments/create/',
                           {'provider': 'alqaseh', 'amount': '5'}, format='json')
    assert response.status_code == 400
    assert not PaymentIntent.objects.exists()


def test_unknown_provider_is_a_400_not_a_500(client):
    user = make_user('pay_unknown')
    client.force_authenticate(user=user)
    response = client.post('/api/payments/create/',
                           {'provider': 'nope', 'amount': '50000'}, format='json')
    assert response.status_code in (400, 502)


def test_methods_lists_only_configured_providers(client, settings):
    user = make_user('pay_methods')
    client.force_authenticate(user=user)

    assert [m['provider'] for m in client.get('/api/payments/methods/').data['data']] == ['alqaseh']

    settings.PAYMENT_PROVIDERS = {'alqaseh': {}}
    reset_cache()
    assert client.get('/api/payments/methods/').data['data'] == []


# ---------------------------------------------------------------------------
# Settling — the part that moves money
# ---------------------------------------------------------------------------

def test_success_credits_the_wallet_once():
    user = make_user('pay_credit')
    intent = make_intent(user)

    with patch.object(type(get_provider('alqaseh')), 'fetch_status',
                      return_value=status_of('succeeded')):
        sync_intent(intent)

    intent.refresh_from_db()
    assert intent.state == PaymentIntent.STATE_SUCCEEDED
    assert intent.applied_at is not None
    assert get_or_create_wallet(user).balance == Decimal('50000')
    assert DepositRequest.objects.filter(user=user, status='completed').count() == 1


def test_settling_twice_credits_once():
    """The webhook and the app's poll routinely both land on a succeeded
    payment. Crediting twice would be the worst bug this feature could have."""
    user = make_user('pay_double')
    intent = make_intent(user)

    with patch.object(type(get_provider('alqaseh')), 'fetch_status',
                      return_value=status_of('succeeded')):
        sync_intent(intent)
        sync_intent(PaymentIntent.objects.get(pk=intent.pk))
        sync_intent(PaymentIntent.objects.get(pk=intent.pk))

    assert get_or_create_wallet(user).balance == Decimal('50000')
    assert DepositRequest.objects.filter(user=user).count() == 1


def test_failed_payment_credits_nothing():
    user = make_user('pay_failed')
    intent = make_intent(user)

    with patch.object(type(get_provider('alqaseh')), 'fetch_status',
                      return_value=status_of('failed')):
        sync_intent(intent)

    intent.refresh_from_db()
    assert intent.state == PaymentIntent.STATE_FAILED
    assert intent.applied_at is None
    assert get_or_create_wallet(user).balance == Decimal('0')


def test_unmapped_gateway_status_is_treated_as_pending():
    """An unrecognised status must never fall through to 'paid'."""
    user = make_user('pay_weird')
    intent = make_intent(user)
    provider = get_provider('alqaseh')

    with patch.object(type(provider), '_request',
                      return_value={'payment_status': 'something_new'}):
        result = provider.fetch_status(intent=intent)

    assert result.state == PaymentIntent.STATE_PENDING


def test_succeeded_with_matching_settlement_is_accepted():
    user = make_user('pay_match')
    intent = make_intent(user)
    provider = get_provider('alqaseh')
    with patch.object(type(provider), '_request', return_value={
        'payment_status': 'succeeded', 'amount': 50000, 'currency': 'IQD',
        'order_id': intent.reference,
    }):
        assert provider.fetch_status(intent=intent).state == PaymentIntent.STATE_SUCCEEDED


@pytest.mark.parametrize('field,value', [
    ('amount', 1000),
    ('amount', 'abc'),
    ('currency', 'USD'),
    ('order_id', 'PI-someone-else'),
])
def test_succeeded_for_a_different_settlement_never_credits(field, value):
    """The gateway sets amount/currency/order from our own create call, so
    a 'succeeded' that disagrees with the intent is a partial capture or the
    wrong payment — never money we may credit in full."""
    user = make_user('pay_mismatch_%s' % field)
    intent = make_intent(user)
    provider = get_provider('alqaseh')
    payload = {'payment_status': 'succeeded', 'amount': 50000, 'currency': 'IQD',
               'order_id': intent.reference, field: value}
    with patch.object(type(provider), '_request', return_value=payload):
        with pytest.raises(ProviderError):
            provider.fetch_status(intent=intent)
        with pytest.raises(ProviderError):
            sync_intent(intent)
    intent.refresh_from_db()
    assert intent.applied_at is None
    assert get_or_create_wallet(user).balance == Decimal('0')


# ---------------------------------------------------------------------------
# Webhooks — untrusted input
# ---------------------------------------------------------------------------

def test_webhook_body_claiming_success_credits_nothing(client):
    """The core security property. Al Qaseh's `p_sign` MAC has no documented
    algorithm, so the payload is unverifiable; the outcome is read back from
    the gateway instead. A forged 'succeeded' body must move no money."""
    user = make_user('pay_forged')
    intent = make_intent(user)

    with patch.object(type(get_provider('alqaseh')), 'fetch_status',
                      return_value=status_of('failed')) as fetch:
        response = client.post(
            '/api/payments/webhook/alqaseh/',
            {'order_id': intent.reference, 'payment_status': 'Success',
             'amount': 999999, 'p_sign': 'whatever'},
            format='json',
        )

    assert response.status_code == 200
    assert fetch.called, "the gateway must be asked, not the payload believed"
    assert get_or_create_wallet(user).balance == Decimal('0')
    intent.refresh_from_db()
    assert intent.state == PaymentIntent.STATE_FAILED


def test_webhook_triggers_a_real_settlement(client):
    user = make_user('pay_hook_ok')
    intent = make_intent(user)

    with patch.object(type(get_provider('alqaseh')), 'fetch_status',
                      return_value=status_of('succeeded')):
        response = client.post('/api/payments/webhook/alqaseh/',
                               {'order_id': intent.reference}, format='json')

    assert response.status_code == 200
    assert get_or_create_wallet(user).balance == Decimal('50000')


def test_webhook_for_unknown_reference_is_a_quiet_200(client):
    """200 on purpose: a different status code would tell a prober which
    references exist, and would make the gateway retry forever."""
    response = client.post('/api/payments/webhook/alqaseh/',
                           {'order_id': 'PI-does-not-exist'}, format='json')
    assert response.status_code == 200


def test_webhook_for_unknown_provider_is_404(client):
    response = client.post('/api/payments/webhook/stripe/', {'order_id': 'x'},
                           format='json')
    assert response.status_code == 404


def test_webhook_needs_no_authentication(client):
    """Gateways call from their own infrastructure with no credentials of ours.
    This is only safe because the callback carries no authority."""
    user = make_user('pay_anon_hook')
    intent = make_intent(user)
    with patch.object(type(get_provider('alqaseh')), 'fetch_status',
                      return_value=status_of('succeeded')):
        response = client.post('/api/payments/webhook/alqaseh/',
                               {'order_id': intent.reference}, format='json')
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def test_detail_syncs_a_pending_payment(client):
    user = make_user('pay_poll')
    intent = make_intent(user)
    client.force_authenticate(user=user)

    with patch.object(type(get_provider('alqaseh')), 'fetch_status',
                      return_value=status_of('succeeded')):
        response = client.get('/api/payments/%s/' % intent.reference)

    assert response.status_code == 200
    assert response.data['data']['state'] == 'succeeded'
    assert get_or_create_wallet(user).balance == Decimal('50000')


def test_detail_of_another_users_payment_is_404(client):
    owner = make_user('pay_owner')
    intent = make_intent(owner)
    stranger = make_user('pay_stranger')
    client.force_authenticate(user=stranger)

    assert client.get('/api/payments/%s/' % intent.reference).status_code == 404


def test_gateway_outage_during_poll_does_not_look_like_failure(client):
    user = make_user('pay_outage')
    intent = make_intent(user)
    client.force_authenticate(user=user)

    with patch.object(type(get_provider('alqaseh')), 'fetch_status',
                      side_effect=ProviderError('timeout')):
        response = client.get('/api/payments/%s/' % intent.reference)

    assert response.status_code == 200
    assert response.data['data']['state'] == 'pending'


# ---------------------------------------------------------------------------
# Provider mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('gateway_status,expected', [
    ('succeeded', 'succeeded'),
    ('declined', 'failed'),
    ('expired', 'failed'),
    ('duplicated', 'failed'),
    ('revoked', 'cancelled'),
    ('prepared', 'pending'),
    ('retried', 'pending'),
    ('unknown', 'pending'),
])
def test_status_mapping(gateway_status, expected):
    user = make_user('pay_map_%s' % gateway_status)
    intent = make_intent(user)
    provider = get_provider('alqaseh')
    with patch.object(type(provider), '_request',
                      return_value={'payment_status': gateway_status}):
        assert provider.fetch_status(intent=intent).state == expected


def test_provider_without_credentials_is_not_configured(settings):
    settings.PAYMENT_PROVIDERS = {'alqaseh': {'BASE_URL': 'x'}}
    reset_cache()
    assert get_provider('alqaseh').is_configured is False


def test_checkout_url_is_built_from_the_token():
    user = make_user('pay_url')
    intent = make_intent(user)
    provider = get_provider('alqaseh')
    with patch.object(type(provider), '_request',
                      return_value={'payment_id': 'p1', 'token': 'tok_abc'}):
        created = provider.create_payment(intent=intent, redirect_url='https://r',
                                          webhook_url='https://w')
    assert created.checkout_url == 'https://pay-test.example/pay/tok_abc'
