"""SRS (Simple Realtime Server) provider — self-hosted WebRTC via WHIP/WHEP.

Wire shape the app receives (see base.py for the envelope):

    publisher:  whip_url, ice_servers, stream_name
    subscriber: whep_url, hls_url, ice_servers, stream_name

Tokens
------
SRS itself trusts nobody: every publish/play carries `?token=…` and SRS calls
back into Django (`on_publish` / `on_play`, see core/live_hooks.py) which
verifies it. A token is `<role>.<stream>.<uid>.<exp>.<hmac>` signed with
LIVE_TOKEN_SECRET — no state, no table, and a leaked publish token dies with
its expiry. The stream *name* is public (it is in every viewer's URL); the
right to publish to it is the token. The token also names the user it was
issued to, so a hook can apply per-user rules (bans) without a lookup table.

TURN
----
Iraqi mobile carriers sit behind CGNAT, so a fair share of viewers cannot
reach the server's UDP candidate directly. coturn runs next to SRS with
`use-auth-secret`; credentials here follow the TURN REST spec
(username = "<exp>:<user>", password = base64(HMAC-SHA1(secret, username)))
so the app gets short-lived TURN access without any state on our side.
"""
import base64
import hashlib
import hmac
import time

from django.conf import settings

from .base import LiveMediaProvider, MediaProviderError

ROLE_PUBLISHER = "publisher"
ROLE_SUBSCRIBER = "subscriber"


def stream_name_of(session):
    """The session's stream id — a random `live_<hex>` fixed at creation."""
    return session.stream_name


def _setting(name, default=""):
    return getattr(settings, name, default)


def _sign(message, secret):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def make_stream_token(stream_name, role, ttl_seconds, *, user_id=0, now=None):
    secret = _setting("LIVE_TOKEN_SECRET")
    if not secret:
        raise MediaProviderError("LIVE_TOKEN_SECRET is not configured")
    exp = int((now if now is not None else time.time()) + ttl_seconds)
    body = "%s.%s.%d.%d" % (role, stream_name, int(user_id or 0), exp)
    return "%s.%s" % (body, _sign(body, secret))


def verify_stream_token(token, *, stream_name, role, now=None):
    """The user id the token was issued to (0 = anonymous) when it is an
    unexpired token signed for exactly this stream+role, else None.
    A publish token never unlocks play and vice versa, so a viewer who
    captures their own token cannot use it to broadcast."""
    secret = _setting("LIVE_TOKEN_SECRET")
    if not secret or not token:
        return None
    try:
        t_role, t_stream, t_uid, t_exp, t_sig = token.split(".", 4)
        t_exp, t_uid = int(t_exp), int(t_uid)
    except ValueError:
        return None
    if t_role != role or t_stream != stream_name:
        return None
    if t_exp < (now if now is not None else time.time()):
        return None
    expected = _sign("%s.%s.%d.%d" % (t_role, t_stream, t_uid, t_exp), secret)
    if not hmac.compare_digest(expected, t_sig):
        return None
    return t_uid


def turn_credentials(user_label, ttl_seconds, *, now=None):
    """Short-lived coturn credentials (TURN REST API / `use-auth-secret`)."""
    secret = _setting("TURN_SECRET")
    host = _setting("TURN_HOST")
    if not secret or not host:
        return []
    exp = int((now if now is not None else time.time()) + ttl_seconds)
    username = "%d:%s" % (exp, user_label)
    password = base64.b64encode(
        hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    ).decode()
    port = _setting("TURN_PORT", 443)
    return [{
        "urls": [
            "turn:%s:%s?transport=udp" % (host, port),
            "turn:%s:%s?transport=tcp" % (host, port),
            "turns:%s:%s?transport=tcp" % (host, port),
        ],
        "username": username,
        "credential": password,
    }]


class SrsProvider(LiveMediaProvider):
    name = "srs"

    @property
    def base_url(self):
        return (_setting("SRS_PUBLIC_BASE_URL") or "").rstrip("/")

    @property
    def app(self):
        return _setting("SRS_APP", "live")

    @property
    def ttl(self):
        return int(_setting("LIVE_TOKEN_TTL_SECONDS", 3600))

    def _require_configured(self):
        if not self.base_url:
            raise MediaProviderError("SRS_PUBLIC_BASE_URL is not configured")

    def _ice_servers(self, user):
        servers = [{"urls": ["stun:%s:%s" % (_setting("TURN_HOST"), _setting("TURN_PORT", 443))]}] \
            if _setting("TURN_HOST") else []
        servers += turn_credentials("u%s" % getattr(user, "id", 0), self.ttl)
        return servers

    def publish_credentials(self, session, user):
        self._require_configured()
        stream = stream_name_of(session)
        token = make_stream_token(stream, ROLE_PUBLISHER, self.ttl,
                                  user_id=getattr(user, "id", 0))
        return {
            "provider": self.name,
            "role": ROLE_PUBLISHER,
            "stream_name": stream,
            "whip_url": "%s/rtc/v1/whip/?app=%s&stream=%s&token=%s"
                        % (self.base_url, self.app, stream, token),
            "ice_servers": self._ice_servers(user),
            "expires_in": self.ttl,
        }

    def play_credentials(self, session, user):
        self._require_configured()
        stream = stream_name_of(session)
        token = make_stream_token(stream, ROLE_SUBSCRIBER, self.ttl,
                                  user_id=getattr(user, "id", 0))
        return {
            "provider": self.name,
            "role": ROLE_SUBSCRIBER,
            "stream_name": stream,
            "whep_url": "%s/rtc/v1/whep/?app=%s&stream=%s&token=%s"
                        % (self.base_url, self.app, stream, token),
            # HLS is the fallback when WebRTC cannot get through at all.
            "hls_url": "%s/%s/%s.m3u8?token=%s" % (self.base_url, self.app, stream, token),
            "ice_servers": self._ice_servers(user),
            "expires_in": self.ttl,
        }
