"""
Phase 0 admin-security regression tests.

These exist to answer one question with certainty before ADMIN_REQUIRE_2FA
is ever flipped on in production: does any of this lock out a legitimate
staff member? Each test isolates one moving part (URL wiring, permission
groups, brute-force lockout, OTP verification) so a future change that
breaks one of them fails loudly here instead of at 2am against real staff
accounts.
"""
import functools

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import Client, RequestFactory
from django.urls import reverse
from django_otp.middleware import is_verified
from django_otp.plugins.otp_totp.models import TOTPDevice
import django_otp

from core.admin_site import BallaAdminSite, OTPBallaAdminSite
from core.models import User

pytestmark = pytest.mark.django_db


def _staff_user(username='staffer', is_staff=True, **extra):
    return User.objects.create_user(
        username=username, password='CorrectHorse123!', is_staff=is_staff, **extra
    )


def _request_with_session(rf, path='/'):
    request = rf.get(path)
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    return request


# ---------------------------------------------------------------------------
# Admin URL is served at the configured path, not the default /admin/
# ---------------------------------------------------------------------------

def test_admin_reverses_to_configured_path():
    assert reverse('admin:index') == f'/{settings.ADMIN_URL_PATH}'


def test_default_admin_path_is_not_exposed():
    if settings.ADMIN_URL_PATH == 'admin/':
        pytest.skip('ADMIN_URL_PATH is still the default in this environment')
    client = Client()
    response = client.get('/admin/')
    assert response.status_code == 404


def test_configured_admin_path_redirects_anonymous_to_two_factor_login():
    # Two hops: admin_view() sends anonymous visitors to admin's own
    # login URL first (Django hardcodes that redirect target), and
    # TWO_FACTOR_PATCH_ADMIN then patches *that* view to redirect again,
    # to settings.LOGIN_URL ('two_factor:login').
    client = Client()
    first = client.get(f'/{settings.ADMIN_URL_PATH}', follow=False)
    assert first.status_code == 302
    assert first.headers['Location'].startswith(f'/{settings.ADMIN_URL_PATH}login/')

    second = client.get(first.headers['Location'], follow=False)
    assert second.status_code == 302
    assert 'account/login' in second.headers['Location']


# ---------------------------------------------------------------------------
# The four scoped groups exist with exactly the permissions the migration
# assigned them — catches accidental drift in core/migrations/0033.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('group_name,expected_codenames', [
    ('الدعم الفني', {'add_dispute', 'change_dispute', 'view_dispute', 'view_user'}),
    ('الشؤون المالية', {'change_withdrawalrequest', 'change_transaction', 'view_wallet'}),
    ('إدارة المحتوى', {'delete_item', 'change_item', 'delete_productreview'}),
    ('عمليات التوصيل', {'change_deliveryorder', 'view_reservation', 'view_location'}),
])
def test_permission_group_has_expected_permissions(group_name, expected_codenames):
    group = Group.objects.get(name=group_name)
    codenames = set(group.permissions.values_list('codename', flat=True))
    assert expected_codenames <= codenames


def test_finance_group_cannot_touch_content_models():
    finance = Group.objects.get(name='الشؤون المالية')
    codenames = set(finance.permissions.values_list('codename', flat=True))
    assert 'delete_item' not in codenames
    assert 'change_item' not in codenames


def test_exactly_four_admin_groups_exist():
    assert set(Group.objects.values_list('name', flat=True)) == {
        'الدعم الفني', 'الشؤون المالية', 'إدارة المحتوى', 'عمليات التوصيل',
    }


# ---------------------------------------------------------------------------
# django-axes: repeated failed logins get locked out — including for the
# *correct* password once the limit is hit, which is the actual protection.
# ---------------------------------------------------------------------------

def test_axes_locks_out_after_repeated_failed_logins():
    from django.contrib.auth import authenticate

    user = _staff_user('lockout-target')
    rf = RequestFactory()

    for _ in range(settings.AXES_FAILURE_LIMIT):
        request = _request_with_session(rf, '/account/login/')
        result = authenticate(request=request, username=user.username, password='wrong-password')
        assert result is None

    # Threshold reached — even the *correct* password must now be refused.
    request = _request_with_session(rf, '/account/login/')
    result = authenticate(request=request, username=user.username, password='CorrectHorse123!')
    assert result is None, 'axes should block the account regardless of a correct password once locked out'


def test_axes_does_not_block_a_user_who_has_not_failed_yet():
    from django.contrib.auth import authenticate

    user = _staff_user('clean-record')
    rf = RequestFactory()
    request = _request_with_session(rf, '/account/login/')

    result = authenticate(request=request, username=user.username, password='CorrectHorse123!')
    assert result is not None and result.pk == user.pk


# ---------------------------------------------------------------------------
# OTP enforcement: OTPBallaAdminSite must refuse an unverified staff session and
# accept a verified one. This is the exact check ADMIN_REQUIRE_2FA turns on.
# ---------------------------------------------------------------------------

def test_otp_admin_site_denies_unverified_staff():
    user = _staff_user('no-device-yet')
    rf = RequestFactory()
    request = _request_with_session(rf, f'/{settings.ADMIN_URL_PATH}')
    request.user = user
    request.user.otp_device = None
    request.user.is_verified = functools.partial(is_verified, request.user)

    assert OTPBallaAdminSite().has_permission(request) is False


def test_otp_admin_site_allows_staff_with_a_confirmed_verified_device():
    user = _staff_user('has-device')
    device = TOTPDevice.objects.create(user=user, name='authenticator', confirmed=True)

    rf = RequestFactory()
    request = _request_with_session(rf, f'/{settings.ADMIN_URL_PATH}')
    request.user = user
    django_otp.login(request, device)
    request.user.is_verified = functools.partial(is_verified, request.user)

    assert OTPBallaAdminSite().has_permission(request) is True


def test_non_staff_user_is_denied_even_when_otp_verified():
    user = _staff_user('verified-but-not-staff', is_staff=False)
    device = TOTPDevice.objects.create(user=user, name='authenticator', confirmed=True)

    rf = RequestFactory()
    request = _request_with_session(rf, f'/{settings.ADMIN_URL_PATH}')
    request.user = user
    django_otp.login(request, device)
    request.user.is_verified = functools.partial(is_verified, request.user)

    assert OTPBallaAdminSite().has_permission(request) is False


# ---------------------------------------------------------------------------
# ADMIN_REQUIRE_2FA stays off until every staff account has enrolled — the
# default admin.site must NOT be OTP-gated unless it was explicitly enabled.
# ---------------------------------------------------------------------------

def test_default_admin_site_is_not_otp_gated_unless_configured():
    from django.contrib import admin
    if settings.ADMIN_REQUIRE_2FA:
        pytest.skip('ADMIN_REQUIRE_2FA is enabled in this environment on purpose')
    assert isinstance(admin.site, BallaAdminSite)
    assert not isinstance(admin.site, OTPBallaAdminSite)
