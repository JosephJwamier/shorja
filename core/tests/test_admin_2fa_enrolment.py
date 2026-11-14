"""An authenticator is bound to a staff account on the server, never the web.

Over the web the only proof of identity at enrolment is the password, so the
first person to know it could bind their own phone and own the admin from
then on. two_factor's setup pages are shadowed (403 plus the command), the OTP
admin sends a device-less staff session to that page instead of looping, and
`manage.py enroll_admin_2fa` is the one way in.
"""
import functools
import io
import re
import time
from base64 import b32decode
from unittest.mock import patch

import pytest
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, RequestFactory
from django_otp import devices_for_user
from django_otp.middleware import is_verified
from django_otp.oath import TOTP
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from core.admin_site import OTPBallaAdminSite
from core.models import User

pytestmark = pytest.mark.django_db

PASSWORD = 'CorrectHorse123!'
KEY_LINE = re.compile(r'^ {4}([A-Z2-7]{32})$', re.M)


@pytest.fixture
def staff():
    return User.objects.create_user(username='boss', password=PASSWORD, is_staff=True)


def _code_for(key_b32):
    totp = TOTP(b32decode(key_b32), 30, 0, 6)
    totp.time = time.time()
    return f'{totp.token():06d}'


def _enrol(username, *args, typed=None):
    """Runs the command the way a person would: reads the key it prints and
    types the code the authenticator would show — or the given [typed]."""
    out = io.StringIO()
    typed = list(typed or [])

    def fake_input(prompt=''):
        if typed:
            return typed.pop(0)
        return _code_for(KEY_LINE.findall(out.getvalue())[-1])

    with patch('builtins.input', fake_input):
        call_command('enroll_admin_2fa', username, *args, stdout=out)
    return out.getvalue()


def test_the_right_code_binds_the_default_device_and_prints_backup_codes(staff):
    output = _enrol('boss')

    device = TOTPDevice.objects.get(user=staff)
    assert device.confirmed and device.name == 'default'
    backup = StaticDevice.objects.get(user=staff, name='backup')
    codes = [t.token for t in backup.token_set.all()]
    assert len(codes) == 10
    assert all(code in output for code in codes)


def test_three_wrong_codes_save_nothing(staff):
    with pytest.raises(CommandError):
        _enrol('boss', typed=['nope', '12', 'abcdef'])
    assert list(devices_for_user(staff, confirmed=None)) == []


def test_a_non_staff_account_is_refused():
    User.objects.create_user(username='shopper', password=PASSWORD)
    with pytest.raises(CommandError):
        _enrol('shopper')


def test_an_enrolled_account_needs_replace_and_replace_drops_the_old_phone(staff):
    _enrol('boss')
    old = TOTPDevice.objects.get(user=staff).pk

    with pytest.raises(CommandError):
        _enrol('boss')

    _enrol('boss', '--replace')
    devices = TOTPDevice.objects.filter(user=staff)
    assert devices.count() == 1 and devices.get().pk != old
    assert StaticDevice.objects.filter(user=staff).count() == 1


@pytest.mark.parametrize('path', ['/account/two_factor/setup/', '/account/two_factor/qrcode/'])
def test_the_web_wizard_cannot_bind_a_device(staff, path):
    client = Client()
    client.force_login(staff)

    r = client.get(path)

    assert r.status_code == 403
    assert b'enroll_admin_2fa boss' in r.content
    assert not TOTPDevice.objects.filter(user=staff).exists()


def _admin_login_request(user):
    request = RequestFactory().get('/panel/login/')
    SessionMiddleware(lambda r: None).process_request(request)
    request.user = user
    user.otp_device = None
    user.is_verified = functools.partial(is_verified, user)
    return request


def test_a_deviceless_staff_session_is_told_the_way_out(staff):
    r = OTPBallaAdminSite().login(_admin_login_request(staff))
    assert r.status_code == 403
    assert b'enroll_admin_2fa' in r.content


def test_an_enrolled_but_unverified_session_goes_to_the_code_step(staff):
    _enrol('boss')
    r = OTPBallaAdminSite().login(_admin_login_request(User.objects.get(pk=staff.pk)))
    assert r.status_code == 302


def test_after_enrolment_the_password_alone_no_longer_signs_in(staff):
    # The device must be named 'default': two_factor asks for a code only
    # when the account has a confirmed device of that name.
    _enrol('boss')
    client = Client()

    r = client.post('/account/login/', {
        'login_view-current_step': 'auth',
        'auth-username': 'boss',
        'auth-password': PASSWORD,
    })

    assert r.status_code == 200          # still on the wizard: the code step
    assert '_auth_user_id' not in client.session
