"""
Rate limits for the authentication endpoints.

Two axes, because one is not enough here:

- **Per client IP** (`ScopedRateThrottle` with a `throttle_scope` on the view)
  caps how fast any one address can hammer an endpoint. Kept deliberately
  generous: Iraqi mobile carriers put thousands of subscribers behind one
  CGNAT address, so a tight per-IP limit would lock out real users, not
  attackers.
- **Per target account** (the throttles below, keyed by the submitted
  username / email) is what actually stops credential stuffing and
  reset-code guessing — an attacker rotating IPs still gets only a handful
  of tries against a given account per window.

Rates live in settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] under the
scope names used here. DRF stores the counters in the default cache (Redis).
"""
import hashlib
import logging

from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle

logger = logging.getLogger(__name__)


class _RequestFieldThrottle(SimpleRateThrottle):
    """Throttle keyed by a field of the POST body instead of the caller.

    Returns None (= no throttling) when the field is absent: the serializer
    will reject such a request anyway, and a missing key must not collapse
    every malformed request into one shared bucket."""
    field = None

    def get_cache_key(self, request, view):
        value = request.data.get(self.field) if hasattr(request.data, 'get') else None
        if not value:
            return None
        ident = str(value).strip().lower()
        return self.cache_format % {'scope': self.scope, 'ident': ident}


class LoginUsernameThrottle(_RequestFieldThrottle):
    """Login attempts against one account, from anywhere. Loose: as tight as
    the one below, anyone who knew a number could keep its owner out."""
    scope = 'auth_login_account_total'
    field = 'username'

    def get_cache_key(self, request, view):
        key = super().get_cache_key(request, view)
        if key is None:
            return None
        # Per account, not per spelling: '0780…', '+964 780…' and the username
        # each had a bucket of their own.
        from .login_identifier import account_for
        account = account_for(request.data.get(self.field))
        if account is None:
            return key
        return self.cache_format % {'scope': self.scope, 'ident': 'account-%s' % account.pk}


class LoginUsernameAddressThrottle(LoginUsernameThrottle):
    """Login attempts against one account from one address."""
    scope = 'auth_login_account'

    def get_cache_key(self, request, view):
        key = super().get_cache_key(request, view)
        return key and '%s-%s' % (key, self.get_ident(request))


class PasswordResetEmailThrottle(_RequestFieldThrottle):
    """Reset-code requests and confirmations for one email — the confirm
    step is a 6-digit code, so this is what makes guessing infeasible."""
    scope = 'auth_reset_email'
    field = 'email'


class _Ceiling(SimpleRateThrottle):
    """A ceiling on everything, high enough that a person never meets it. Read
    from settings on each request, and open when the cache is down: a limit
    that takes the site down with it is worse than none."""

    def get_rate(self):
        return settings.REQUEST_RATES[self.scope]

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.warning('request ceiling skipped', exc_info=True)
            return True

    def member(self, request):
        return bool(request.user and request.user.is_authenticated)


class VisitorAddressThrottle(_Ceiling):
    scope = 'visitor_address'

    def get_cache_key(self, request, view):
        if self.member(request):
            return None
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class VisitorDeviceThrottle(_Ceiling):
    """The address and what the browser says it is: one phone behind a carrier's
    shared address does not use up its neighbours'."""
    scope = 'visitor_device'

    def get_cache_key(self, request, view):
        if self.member(request):
            return None
        device = hashlib.sha256((self.get_ident(request) + '|' + request.META.get(
            'HTTP_USER_AGENT', '')[:200]).encode()).hexdigest()[:24]
        return self.cache_format % {'scope': self.scope, 'ident': device}


class MemberThrottle(_Ceiling):
    scope = 'member'

    def get_cache_key(self, request, view):
        if not self.member(request):
            return None
        return self.cache_format % {'scope': self.scope, 'ident': request.user.pk}


class PaymentThrottle(UserRateThrottle):
    """Each one opens a page at the gateway, which counts them too."""
    scope = 'payments'


class InstallmentThrottle(UserRateThrottle):
    """Per buyer, on every installment call that carries a card or account
    number. Without it the eligibility check is an oracle: try numbers against
    one pending reservation and learn which are real Qi cards, and whose."""
    scope = 'installments'
