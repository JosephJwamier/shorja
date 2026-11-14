"""A brute-force lockout must hit only the account being guessed from the
address guessing it — never the whole platform.

Behind Caddy every request reaches Django with the same REMOTE_ADDR (Caddy's
container). With AXES_LOCKOUT_PARAMETERS = ['username', 'ip_address'] (lock on
either), five failures anywhere locked that one shared IP, i.e. everyone. The
fix locks on the (username, ip_address) pair and reads the real client IP from
the X-Forwarded-For that Caddy appends.

transaction=True: axes writes AccessAttempt rows through its own DB handler.
"""
import pytest
from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory

from core.models import User

pytestmark = pytest.mark.django_db(transaction=True)

CADDY_IP = '172.18.0.5'          # what every request's REMOTE_ADDR is in prod
PASSWORD = 'CorrectHorse123!'


@pytest.fixture(autouse=True)
def clear_axes():
    from axes.utils import reset
    reset()
    yield
    reset()


def _req(client_ip):
    # Caddy always appends the peer address to X-Forwarded-For; REMOTE_ADDR is
    # Caddy itself. The real client is the XFF value.
    rf = RequestFactory()
    request = rf.post('/api/auth/login/', REMOTE_ADDR=CADDY_IP,
                      HTTP_X_FORWARDED_FOR=client_ip)
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    return request


def _fail_five(username, client_ip):
    for _ in range(settings.AXES_FAILURE_LIMIT):
        assert authenticate(request=_req(client_ip),
                            username=username, password='wrong') is None


def test_the_real_client_ip_is_read_from_x_forwarded_for():
    from axes.helpers import get_client_ip_address
    assert get_client_ip_address(_req('203.0.113.9')) == '203.0.113.9'


def test_guessing_one_account_does_not_lock_a_neighbour_on_the_same_proxy():
    attacker_ip = '203.0.113.9'
    _staff = User.objects.create_user(username='victim', password=PASSWORD)
    neighbour = User.objects.create_user(username='neighbour', password=PASSWORD)

    _fail_five('victim', attacker_ip)

    # Same Caddy REMOTE_ADDR, a different real client: must still get in.
    ok = authenticate(request=_req('203.0.113.50'),
                      username='neighbour', password=PASSWORD)
    assert ok is not None and ok.pk == neighbour.pk


def test_the_victim_can_still_sign_in_from_their_own_phone():
    victim = User.objects.create_user(username='victim', password=PASSWORD)

    _fail_five('victim', '203.0.113.9')   # attacker's IP

    ok = authenticate(request=_req('203.0.113.77'),   # victim's real IP
                      username='victim', password=PASSWORD)
    assert ok is not None and ok.pk == victim.pk


def test_the_guessing_pair_itself_is_locked():
    User.objects.create_user(username='victim', password=PASSWORD)
    attacker_ip = '203.0.113.9'

    _fail_five('victim', attacker_ip)

    # Correct password, but from the address that was guessing: refused.
    assert authenticate(request=_req(attacker_ip),
                        username='victim', password=PASSWORD) is None
