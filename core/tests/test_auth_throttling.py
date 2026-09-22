"""
Rate limits on the authentication endpoints (core/throttling.py).

The point of each test is the *axis* being limited: per submitted account
regardless of IP, or per IP regardless of account. Rates are the real ones
from settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']; conftest.py gives
every test a fresh in-memory cache so counters never bleed between tests.
"""
import pytest
from rest_framework.test import APIClient

from core.models import User

pytestmark = pytest.mark.django_db

LOGIN = '/api/auth/login/'
TOKEN = '/api/auth/token/'
RESET = '/api/auth/reset-password/'
RESET_CONFIRM = '/api/auth/reset-password-confirm/'
REGISTER = '/api/auth/register/'


def login(client, username, ip='10.0.0.1', password='wrong-password'):
    return client.post(LOGIN, {'username': username, 'password': password},
                       format='json', REMOTE_ADDR=ip)


class TestLoginPerAccount:
    def test_sixth_attempt_against_one_username_is_throttled(self):
        client = APIClient()
        for _ in range(5):
            assert login(client, 'victim').status_code != 429
        assert login(client, 'victim').status_code == 429

    def test_rotating_ips_does_not_reset_the_account_bucket(self):
        # The per-username throttle is what defeats an attacker who rotates
        # addresses — five tries from five IPs still exhausts the budget.
        client = APIClient()
        for i in range(5):
            login(client, 'victim', ip=f'10.0.0.{i + 1}')
        assert login(client, 'victim', ip='10.0.0.99').status_code == 429

    def test_other_accounts_are_unaffected(self):
        client = APIClient()
        for _ in range(6):
            login(client, 'victim')
        assert login(client, 'someone-else').status_code != 429

    def test_username_bucket_is_case_insensitive(self):
        client = APIClient()
        for _ in range(5):
            login(client, 'Victim')
        assert login(client, 'VICTIM').status_code == 429

    def test_successful_login_within_budget_still_works(self):
        User.objects.create_user(username='real', password='pass12345', phone='07700000001')
        client = APIClient()
        for _ in range(4):
            login(client, 'real')
        response = login(client, 'real', password='pass12345')
        assert response.status_code == 200

    def test_jwt_token_endpoint_shares_the_same_budget(self):
        client = APIClient()
        for _ in range(5):
            login(client, 'victim')
        response = client.post(TOKEN, {'username': 'victim', 'password': 'x'},
                               format='json', REMOTE_ADDR='10.0.0.1')
        assert response.status_code == 429


class TestLoginPerIp:
    def test_one_ip_spraying_many_usernames_is_throttled(self):
        client = APIClient()
        for i in range(60):
            assert login(client, f'user{i}', ip='10.0.0.7').status_code != 429
        assert login(client, 'user-final', ip='10.0.0.7').status_code == 429

    def test_other_ips_keep_working_when_one_is_throttled(self):
        # Iraqi CGNAT means a whole carrier can share one address, so the
        # per-IP budget is generous — but it must still be per IP.
        client = APIClient()
        for i in range(61):
            login(client, f'user{i}', ip='10.0.0.7')
        assert login(client, 'fresh', ip='10.0.0.8').status_code != 429


class TestPasswordResetPerEmail:
    def test_sixth_reset_request_for_one_email_is_throttled(self):
        client = APIClient()
        for _ in range(5):
            r = client.post(RESET, {'email': 'a@example.com'}, format='json')
            assert r.status_code != 429
        assert client.post(RESET, {'email': 'a@example.com'}, format='json').status_code == 429

    def test_request_and_confirm_share_one_bucket_per_email(self):
        # Guessing the 6-digit code happens on the confirm endpoint; it must
        # not have a separate budget from the request endpoint.
        client = APIClient()
        for _ in range(5):
            client.post(RESET, {'email': 'a@example.com'}, format='json')
        r = client.post(RESET_CONFIRM, {
            'email': 'a@example.com', 'code': '000000',
            'new_password': 'newpass123', 'new_password2': 'newpass123',
        }, format='json')
        assert r.status_code == 429

    def test_missing_email_does_not_share_a_bucket(self):
        # A malformed body has no email; it must be rejected by validation,
        # not collapse every malformed request into one throttled key.
        client = APIClient()
        for _ in range(7):
            r = client.post(RESET, {}, format='json')
            assert r.status_code != 429


class TestRegisterPerIp:
    def test_registration_is_throttled_per_ip(self):
        client = APIClient()
        for i in range(30):
            r = client.post(REGISTER, {'username': f'u{i}'}, format='json', REMOTE_ADDR='10.0.0.3')
            assert r.status_code != 429
        r = client.post(REGISTER, {'username': 'u-final'}, format='json', REMOTE_ADDR='10.0.0.3')
        assert r.status_code == 429
