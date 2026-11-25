"""Ending a user's sessions on every device.

A refresh token lives 7 days and is not tied to the password, so changing the
password used to change nothing for whoever already held one: someone who had
stolen a token stayed inside the account after its owner "secured" it.
Blacklisting every outstanding refresh token is what actually signs the other
devices out.

An access token is verified by its signature, so it used to outlive that by up to
30 minutes. `tokens_valid_after` ends those too: the user row is read on every
request anyway, so comparing one more column costs nothing.
"""
from django.contrib.auth.models import update_last_login
from django.utils import timezone
from rest_framework import exceptions
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.serializers import (
    TokenObtainPairSerializer, TokenObtainSerializer,
)
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken, OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken

from .password_rules import (
    WEAK_PASSWORD_CODE, WEAK_PASSWORD_MESSAGE_PORTAL, password_error,
)


class PolicyTokenObtainPairSerializer(TokenObtainPairSerializer):
    """/api/auth/token/ (the delivery portal's sign-in) refuses what
    /api/auth/login/ refuses: a correct password that today's policy rejects.

    The check sits between authenticating and minting, so a refused sign-in
    leaves no token behind. That means repeating the parent's three minting
    lines (simplejwt 5.3) instead of calling it — the parent mints first.
    """

    def validate(self, attrs):
        data = TokenObtainSerializer.validate(self, attrs)  # sets self.user
        if password_error(attrs['password'], user=self.user):
            raise exceptions.PermissionDenied(
                WEAK_PASSWORD_MESSAGE_PORTAL, code=WEAK_PASSWORD_CODE)

        refresh = self.get_token(self.user)
        data['refresh'] = str(refresh)
        data['access'] = str(refresh.access_token)
        if api_settings.UPDATE_LAST_LOGIN:
            update_last_login(None, self.user)
        return data


def revoke_refresh_tokens(user):
    """Blacklists every refresh token issued to [user]. Returns how many."""
    live = OutstandingToken.objects.filter(user=user, blacklistedtoken__isnull=True)
    rows = [BlacklistedToken(token=token) for token in live]
    BlacklistedToken.objects.bulk_create(rows, ignore_conflicts=True)
    user.tokens_valid_after = timezone.now()
    type(user).objects.filter(pk=user.pk).update(tokens_valid_after=user.tokens_valid_after)
    return len(rows)


def token_is_stale(user, token):
    """Issued before this user's sessions were ended? `iat` is in whole seconds,
    so the pair minted right after the change is on the good side of it."""
    after = user.tokens_valid_after
    return after is not None and int(token.get('iat', 0)) < int(after.timestamp())


class SessionJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if token_is_stale(user, validated_token):
            raise exceptions.AuthenticationFailed('انتهت الجلسة — سجّل الدخول من جديد',
                                                  code='session_ended')
        return user


def fresh_token_pair(user):
    """A new pair for the device that made the change, issued after revoking,
    so it is the only session left."""
    refresh = RefreshToken.for_user(user)
    return {'access': str(refresh.access_token), 'refresh': str(refresh)}
