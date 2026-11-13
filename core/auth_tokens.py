"""Ending a user's sessions on every device.

A refresh token lives 7 days and is not tied to the password, so changing the
password used to change nothing for whoever already held one: someone who had
stolen a token stayed inside the account after its owner "secured" it.
Blacklisting every outstanding refresh token is what actually signs the other
devices out.

An access token already issued keeps working until it expires
(ACCESS_TOKEN_LIFETIME, 30 minutes): simplejwt verifies those without the
database. That window is the price of stateless access tokens, and it is short.
"""
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken, OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken


def revoke_refresh_tokens(user):
    """Blacklists every refresh token issued to [user]. Returns how many."""
    live = OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True)
    rows = [BlacklistedToken(token=token) for token in live]
    BlacklistedToken.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def fresh_token_pair(user):
    """A new pair for the device that made the change, issued after revoking,
    so it is the only session left."""
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}
