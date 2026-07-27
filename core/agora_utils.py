from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from agora_token_builder import RtcTokenBuilder

ROLE_PUBLISHER = 1
ROLE_SUBSCRIBER = 2


def build_rtc_token(channel_name, uid=0, role='subscriber'):
    """
    Generate an Agora RTC join token locally (HMAC-based, no network call).
    Video/audio media itself never passes through Django — this token is
    only what lets a Flutter client join the Agora channel directly.
    """
    ttl = getattr(settings, 'AGORA_TOKEN_TTL_SECONDS', 3600)
    expire_ts = int(timezone.now().timestamp()) + ttl
    agora_role = ROLE_PUBLISHER if role == 'publisher' else ROLE_SUBSCRIBER

    token = RtcTokenBuilder.buildTokenWithUid(
        settings.AGORA_APP_ID,
        settings.AGORA_APP_CERTIFICATE,
        channel_name,
        uid,
        agora_role,
        expire_ts,
    )
    return token, ttl, timezone.now() + timedelta(seconds=ttl)
