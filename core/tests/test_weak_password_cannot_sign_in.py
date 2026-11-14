"""A correct password that today's policy rejects no longer opens an account.

Accounts made before the policy was enforced still had passwords like
"12345678", their username, or their phone number — 7 of 11 at the 2026-11
pentest, and usernames and phones were on public display. Both sign-in doors,
/api/auth/login/ (the app) and /api/auth/token/ (the delivery portal), now
refuse such a password with a code the app shows as "reset it", and mint no
token. The WhatsApp reset applies the policy, so that way out works.
"""
import pytest
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from core.models import User
from core.password_rules import WEAK_PASSWORD_CODE, password_error

pytestmark = pytest.mark.django_db

LOGIN = '/api/auth/login/'
TOKEN = '/api/auth/token/'
PHONE = '+9647701234567'
STRONG = 'Kahraman-2026!'


def _account(password, **extra):
    # create_user skips the validators — exactly how pre-policy accounts
    # came to exist.
    return User.objects.create_user(
        username='oldtimer', password=password, phone=PHONE, **extra)


def _login(password, door=LOGIN):
    return APIClient().post(door, {'username': 'oldtimer', 'password': password},
                            format='json')


@pytest.mark.parametrize('weak', ['12345678', 'oldtimer1', '07701234567'])
def test_the_app_door_refuses_a_correct_but_weak_password(weak):
    _account(weak)

    r = _login(weak)

    assert r.status_code == 403
    assert r.data['code'] == WEAK_PASSWORD_CODE
    assert 'نسيت كلمة المرور' in r.data['error']
    assert 'tokens' not in r.data
    assert OutstandingToken.objects.count() == 0


def test_a_wrong_password_is_still_plain_invalid_credentials():
    _account('12345678')
    assert _login('87654321').status_code == 401


def test_a_strong_password_signs_in_as_before():
    _account(STRONG)
    r = _login(STRONG)
    assert r.status_code == 200
    assert r.data['tokens']['access']


def test_the_sign_in_response_carries_the_users_own_contact():
    # The request is anonymous while the response is built, so without an
    # explicit viewer the serializer stripped the user's own email and phone —
    # and the app stores this object as the signed-in user.
    _account(STRONG, email='me@example.com')
    user = _login(STRONG).data['user']
    assert user['phone'] == PHONE
    assert user['email'] == 'me@example.com'


def test_the_portal_door_refuses_it_too_and_mints_nothing():
    _account('12345678')

    r = _login('12345678', door=TOKEN)

    assert r.status_code == 403
    assert 'access' not in r.data
    assert OutstandingToken.objects.count() == 0


def test_the_portal_door_still_signs_in_a_strong_password():
    _account(STRONG)
    r = _login(STRONG, door=TOKEN)
    assert r.status_code == 200
    assert r.data['access'] and r.data['refresh']


def test_the_phone_number_is_not_a_password_even_with_its_plus():
    # '+' dodges NumericPasswordValidator; the similarity check on 'phone'
    # is what catches it.
    user = User(username='someone', phone=PHONE)
    assert password_error('+9647701234567', user=user)
    assert password_error('ali07701234567', user=user)
    assert password_error('Kahraman-2026!', user=user) is None
