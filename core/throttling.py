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
from rest_framework.throttling import SimpleRateThrottle


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
    """Failed-or-not login attempts against one username."""
    scope = 'auth_login_account'
    field = 'username'


class PasswordResetEmailThrottle(_RequestFieldThrottle):
    """Reset-code requests and confirmations for one email — the confirm
    step is a 6-digit code, so this is what makes guessing infeasible."""
    scope = 'auth_reset_email'
    field = 'email'
