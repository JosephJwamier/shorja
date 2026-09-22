"""
VerifyPhoneView — the Firebase Phone Auth path.

Firebase generates and delivers the SMS from the device, so there is no
server-side code to compare against; the server verifies the resulting ID
token and confirms the phone inside it belongs to the calling account. Every
Firebase call is mocked at the firebase_admin boundary — no network here.

Mirrors test_reels.py's pytest-django + APIClient style.
"""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from core.auth_views import normalize_phone_suffix
from core.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def make_user(username, phone='07701234567', **kwargs):
    return User.objects.create_user(
        username=username, password='pass12345', phone=phone, **kwargs
    )


def firebase_ok(phone_number):
    """Patches both the Firebase availability gate and the token decode."""
    return (
        patch('core.firebase.get_firebase_app', return_value=object()),
        patch('firebase_admin.auth.verify_id_token',
              return_value={'uid': 'fb-uid', 'phone_number': phone_number}),
    )


def post_token(client, phone_number, token='tok'):
    app_patch, verify_patch = firebase_ok(phone_number)
    with app_patch, verify_patch:
        return client.post('/api/auth/verify-phone/', {'id_token': token}, format='json')


# ---------------------------------------------------------------------------
# normalize_phone_suffix
# ---------------------------------------------------------------------------

def test_local_and_e164_forms_normalize_to_the_same_suffix():
    assert normalize_phone_suffix('07701234567') == normalize_phone_suffix('+9647701234567')
    assert normalize_phone_suffix('+964 770 123 4567'.replace(' ', '')) == '701234567'


def test_too_short_or_missing_normalizes_to_empty():
    assert normalize_phone_suffix('') == ''
    assert normalize_phone_suffix(None) == ''
    assert normalize_phone_suffix('12345') == ''


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_matching_e164_token_verifies_a_locally_stored_number(client):
    user = make_user('ph_ok', phone='07701234567')
    client.force_authenticate(user=user)

    response = post_token(client, '+9647701234567')

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.phone_verified is True


def test_verification_does_not_touch_firebase_uid(client):
    """firebase_uid identifies the user's Google/Facebook identity for
    SocialLoginView — a phone-only UID must not overwrite it."""
    user = make_user('ph_uid', phone='07701234567', firebase_uid='google-uid')
    client.force_authenticate(user=user)

    post_token(client, '+9647701234567')

    user.refresh_from_db()
    assert user.firebase_uid == 'google-uid'


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------

def test_token_for_a_different_number_is_rejected(client):
    """The token proves someone owns *that* phone, not that it's this
    account's phone — without the match check any verified number would
    verify any account."""
    user = make_user('ph_mismatch', phone='07701234567')
    client.force_authenticate(user=user)

    response = post_token(client, '+9647809999999')

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.phone_verified is False


def test_token_without_a_phone_claim_is_rejected(client):
    user = make_user('ph_nophone', phone='07701234567')
    client.force_authenticate(user=user)

    response = post_token(client, None)

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.phone_verified is False


def test_account_without_a_phone_is_rejected(client):
    user = make_user('ph_blank', phone='')
    client.force_authenticate(user=user)

    response = post_token(client, '+9647701234567')

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.phone_verified is False


def test_number_already_verified_on_another_account_is_rejected(client):
    make_user('ph_first', phone='07701234567', phone_verified=True)
    second = make_user('ph_second', phone='07701234567')
    client.force_authenticate(user=second)

    response = post_token(client, '+9647701234567')

    assert response.status_code == 409
    second.refresh_from_db()
    assert second.phone_verified is False


def test_invalid_token_is_rejected(client):
    user = make_user('ph_badtoken', phone='07701234567')
    client.force_authenticate(user=user)

    with patch('core.firebase.get_firebase_app', return_value=object()), \
            patch('firebase_admin.auth.verify_id_token', side_effect=ValueError('bad token')):
        response = client.post('/api/auth/verify-phone/', {'id_token': 'tok'}, format='json')

    assert response.status_code == 401
    user.refresh_from_db()
    assert user.phone_verified is False


def test_unconfigured_firebase_reports_503(client):
    user = make_user('ph_nofirebase', phone='07701234567')
    client.force_authenticate(user=user)

    with patch('core.firebase.get_firebase_app', return_value=None):
        response = client.post('/api/auth/verify-phone/', {'id_token': 'tok'}, format='json')

    assert response.status_code == 503
    user.refresh_from_db()
    assert user.phone_verified is False


def test_anonymous_request_is_rejected(client):
    response = client.post('/api/auth/verify-phone/', {'id_token': 'tok'}, format='json')
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Legacy code path still works
# ---------------------------------------------------------------------------

def test_legacy_six_digit_code_still_verifies(client):
    """An older build in the wild still posts {'code': ...} — that must keep
    working alongside the Firebase path."""
    user = make_user('ph_legacy', phone='07701234567')
    code = user.generate_verification_code('phone')
    client.force_authenticate(user=user)

    response = client.post('/api/auth/verify-phone/', {'code': code}, format='json')

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.phone_verified is True
