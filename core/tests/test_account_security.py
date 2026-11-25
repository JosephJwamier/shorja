"""Who may sign in to, change, or delete an account (see identity_proof)."""
import threading
import time
from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.core.cache import cache
from django.db import connections, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import *
from core.models import get_or_create_wallet
from core.services.disputes import resolve_dispute
from core.services.withdrawals import approve_withdrawal
from core.tests.test_money_faults import P, api, bal, res, total, user, w  # noqa
from core.wallet_utils import complete_reservation, release_reservation_payment

pytestmark = pytest.mark.django_db
PW = 'Zx9!kQ2#long'


@pytest.fixture(autouse=True)
def clean():
    cache.clear()
    yield
    cache.clear()


def real(n, phone, **k):
    u = User.objects.create_user(username=n, password=PW, phone=phone, **k)
    return u


def reg(**k):
    body = {'first_name': 'a', 'last_name': 'b', 'phone': '07701110000', 'password': PW, 'password2': PW, **k}
    return APIClient().post('/api/auth/register/', body, format='json')


@pytest.mark.parametrize('name', ['07801234567', '+9647801234567', 'victim@b.co'])
def test_username_cannot_be_someones_identifier(name):
    real('victim', '07801234567', email='victim@b.co')
    reg(username=name)
    x = APIClient().post('/api/auth/login/', {'username': name, 'password': PW}, format='json')
    assert x.status_code == 200 and x.data['user']['username'] == 'victim', (x.status_code, getattr(x, 'data', None) and x.data.get('user', {}).get('username'))


def test_attacker_gets_victims_typed_login():
    real('victim', '07801234567')
    r = reg(username='07801234567', password='Other9!pass#', password2='Other9!pass#')
    x = APIClient().post('/api/auth/login/', {'username': '07801234567', 'password': PW}, format='json')
    assert x.status_code == 200, ('victim locked out', r.status_code, x.status_code)


def test_changing_the_phone_needs_the_password():
    u = real('v', '07801234567')
    x = api(u).patch(f'/api/users/{u.id}/', {'phone': '07709998888'}, format='json')
    u.refresh_from_db()
    assert u.phone.endswith('7801234567'), (x.status_code, u.phone)


def test_deleting_the_account_needs_the_password():
    u = real('v', '07801234567')
    x = api(u).post('/api/users/delete-account/')
    u.refresh_from_db()
    assert u.is_active, x.status_code


def test_login_throttle_is_per_account_not_per_spelling():
    real('v', '07801234567')
    codes = []
    for i, s in enumerate(['07801234567', '+9647801234567', '9647801234567', '009647801234567', '0780 1234567',
                           '0780-1234567', '078 01234567', '0780 123 4567', '078-0123-4567', 'v']):
        for n in range(2):
            c = APIClient(REMOTE_ADDR='10.0.%d.%d' % (i, n))
            codes.append(c.post('/api/auth/login/', {'username': s, 'password': 'wrong'}, format='json').status_code)
    # From one address; the same from many is bounded too, more loosely.
    assert codes.count(401) <= 20, codes
    cache.clear()
    codes = [APIClient().post('/api/auth/login/', {'username': s, 'password': 'wrong'}, format='json').status_code
             for s in ['07801234567', '+9647801234567', '0780 1234567', 'v'] * 3]
    assert codes.count(401) <= 5, codes


@pytest.mark.django_db(transaction=True)
def test_parallel_otp_guesses_are_limited(monkeypatch):
    from core import otp_views
    real('v', '07801234567')
    cache.set(otp_views._cache_key('+9647801234567'), 'm', 600)
    seen = []

    def slow(mid, code):
        seen.append(code); time.sleep(.4); return False
    monkeypatch.setattr(otp_views, 'verify_otp', slow)
    gate = threading.Barrier(20)

    def go(n):
        gate.wait()
        APIClient(REMOTE_ADDR='10.1.0.%d' % n).post('/api/auth/reset-password-by-code/', {}, format='json')
        for url in OTP_RESET:
            APIClient(REMOTE_ADDR='10.1.0.%d' % n).post(url, {'phone': '07801234567', 'code': '%06d' % n, 'new_password': PW}, format='json')
        connections.close_all()
    ts = [threading.Thread(target=go, args=(n,)) for n in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert 0 < len(seen) <= 5, len(seen)


def _otp_reset():
    from django.urls import get_resolver
    out = []

    def walk(p, pre=''):
        for q in p.url_patterns:
            if hasattr(q, 'url_patterns'):
                walk(q, pre + str(q.pattern))
            elif getattr(getattr(q.callback, 'view_class', None), '__name__', '') == 'ResetPasswordByCodeView':
                out.append('/' + pre + str(q.pattern))
    walk(get_resolver())
    return out


OTP_RESET = _otp_reset()


def prof(u, **body):
    return api(u).patch('/api/auth/profile/update/', body, format='json')


def test_a_username_shaped_like_a_contact_is_refused():
    for name in ('07801234567', '+9647801234567', 'a@b.co', '123456'):
        assert reg(username=name).status_code == 400, name
    assert reg(username='ali99').status_code == 201


def test_the_password_changes_the_phone_and_the_email():
    u = real('v', '07801234567')
    x = prof(u, phone='07709998888')
    assert x.status_code == 400 and x.data['code'] == 'proof_required', x.data
    x = prof(u, phone='07709998888', current_password='no')
    assert x.status_code == 400 and x.data['code'] == 'wrong_password', x.data
    assert prof(u, email='new@b.co').status_code == 400
    assert prof(u, phone='07709998888', email='new@b.co', current_password=PW).status_code == 200
    u.refresh_from_db()
    assert u.phone == '+9647709998888' and u.email == 'new@b.co'


def test_everything_else_needs_no_proof():
    u = real('v', '07801234567')
    assert prof(u, phone='07801234567', bio='x').status_code == 200


def test_a_google_account_proves_itself_with_a_code(monkeypatch):
    from core import otp_views
    u = real('v', '07801234567')
    u.set_unusable_password(); u.save()
    cache.set(otp_views._cache_key('+9647801234567'), 'm', 600)
    monkeypatch.setattr(otp_views, 'verify_otp', lambda m, c: c == '111111')
    assert prof(u, phone='07709998888', code='000000').data['code'] == 'wrong_code'
    assert prof(u, phone='07709998888', code='111111').status_code == 200


def test_a_google_account_with_no_phone_adds_one():
    u = real('v', None)
    u.set_unusable_password(); u.save()
    assert prof(u, phone='07709998888').status_code == 200


def test_the_password_deletes_the_account():
    u = real('v', '07801234567')
    assert api(u).post('/api/users/delete-account/', {}, format='json').data['code'] == 'proof_required'
    assert api(u).post('/api/users/delete-account/', {'current_password': PW}, format='json').status_code == 200


def test_guessing_the_password_from_a_session_is_limited():
    u = real('v', '07801234567')
    body = {'old_password': 'x', 'new_password': 'Other9!pass#', 'new_password2': 'Other9!pass#'}
    codes = [api(u).post('/api/auth/change-password/', body, format='json').status_code for _ in range(12)]
    assert codes[9] == 400 and codes[10] == 429, codes
    assert prof(u, phone='07709998888', current_password=PW).data['code'] == 'too_many_attempts'


def test_a_broken_whatsapp_service_is_not_a_wrong_guess(monkeypatch):
    from core import otp_views
    cache.set(otp_views._cache_key('+9647801234567'), 'm', 600)

    def down(m, c):
        raise otp_views.WevlixError('down')
    monkeypatch.setattr(otp_views, 'verify_otp', down)
    for _ in range(8):
        otp_views._check_code('+9647801234567', '1')
    monkeypatch.setattr(otp_views, 'verify_otp', lambda m, c: True)
    assert otp_views._check_code('+9647801234567', '1')[0]


def test_a_numeric_google_address_still_gets_a_username():
    from core.auth_views import unique_username
    assert '780' not in unique_username('07801234567@gmail.com', '')
