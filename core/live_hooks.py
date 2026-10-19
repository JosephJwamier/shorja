"""SRS → Django callbacks (http_hooks in infra/live/srs.conf).

Routed at `live/srs/<event>/` (core/urls.py). SRS POSTs a JSON body like
    {"action": "on_publish", "client_id": "…", "ip": "…", "vhost": "…",
     "app": "live", "stream": "live_ab12…", "param": "?token=…"}
and only proceeds if we answer `{"code": 0}`. Anything else (or a non-200)
rejects the publish/play. Two layers of authority:

  1. `?secret=` on the hook URL — proves the caller is *our* SRS, not a
     stranger on the internet (the URL is only in srs.conf on the box).
  2. `token` inside `param` — proves the *client* was issued the right to
     do this by Django (see services/live_media/srs.py). A publish token is
     only ever minted for the store owner, so on_publish needs nothing more
     than "valid publish token for this stream".

on_play is where product policy lives — see _may_play().

HLS does not go through any of this: playlists and segments are plain files
served by SRS's own http_server, and no hook fires for them. Caddy asks
HlsAuthView about each one instead — see the bottom of this file.
"""
import logging
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import LiveSession
from .services.live_media.srs import ROLE_PUBLISHER, ROLE_SUBSCRIBER, verify_stream_token

logger = logging.getLogger(__name__)

OK = {"code": 0}
DENY = {"code": 1}


def _token_from_param(param):
    # SRS passes the stream's query string verbatim, e.g. "?token=abc&x=1".
    values = parse_qs((param or "").lstrip("?"))
    return (values.get("token") or [""])[0]


def _may_play(session, user_id):
    """Whether the viewer named by a valid play token may watch `session`.

    Three rules, strictest of the options we weighed, because each one is
    load-bearing somewhere else in the product:

    1. **An account.** `user_id` is 0 for a token minted for an anonymous
       caller. Refusing those is what makes viewer counts, moderation and
       "who reserved during the stream" mean anything — a ban is useless
       if the banned viewer can come back as nobody. The app already signs
       every request, so this costs no real viewer anything.
    2. **The session is live.** A stream name lives in every viewer's URL
       and does not change when the session ends, so without this a stale
       token would keep replaying a finished broadcast — and once DVR is
       on, the recording too. Watching is a right over a *session*, not
       over a name.
    3. **Not banned.** The seller's own moderation, enforced on the media
       server rather than only in the UI: a ban cuts the picture, not just
       the comment box.

    Rules 1 and 2 are cheap field reads; only the ban check touches the
    database, and only for viewers who passed the first two.
    """
    if not user_id:
        return False
    if session.status != 'live':
        return False
    return not session.bans.filter(user_id=user_id).exists()


class SrsHookView(APIView):
    """One view for every SRS event; the URL's <event> selects the handler."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, event):
        if request.query_params.get("secret", "") != getattr(settings, "SRS_HOOK_SECRET", None):
            # Wrong or missing shared secret: not our SRS. Deny, and say
            # nothing useful.
            return Response(DENY, status=403)

        body = request.data if isinstance(request.data, dict) else {}
        stream = str(body.get("stream") or "")
        token = _token_from_param(body.get("param"))
        handler = getattr(self, "handle_%s" % event, None)
        if handler is None:
            return Response(DENY, status=404)

        session = LiveSession.objects.filter(stream_name=stream).first()
        if session is None:
            logger.warning("srs %s for unknown stream %r", event, stream)
            return Response(DENY)
        return handler(session, token, body)

    # -- publishing ------------------------------------------------------
    def handle_on_publish(self, session, token, body):
        user_id = verify_stream_token(token, stream_name=session.stream_name,
                                      role=ROLE_PUBLISHER)
        if user_id is None:
            logger.warning("srs on_publish denied for %s (bad token)", session.id)
            return Response(DENY)
        # 'scheduled' as well as 'live': the owner's app opens the camera
        # before it flips the session live, and a seller checking their
        # framing first should not be refused. Watching is stricter — see
        # _may_play.
        if session.status not in ("scheduled", "live"):
            return Response(DENY)
        return Response(OK)

    def handle_on_unpublish(self, session, token, body):
        # Informational: the phone dropped or the seller stopped. The
        # session's own status is driven by the app's end action; here we
        # only note the time so support can see gaps.
        logger.info("srs on_unpublish %s at %s", session.id, timezone.now().isoformat())
        return Response(OK)

    # -- watching --------------------------------------------------------
    def handle_on_play(self, session, token, body):
        user_id = verify_stream_token(token, stream_name=session.stream_name,
                                      role=ROLE_SUBSCRIBER)
        if user_id is None:
            return Response(DENY)
        return Response(OK if _may_play(session, user_id) else DENY)

    def handle_on_stop(self, session, token, body):
        return Response(OK)

    # -- recording -------------------------------------------------------
    def handle_on_dvr(self, session, token, body):
        # SRS finished writing an MP4 (dvr_plan session). Upload to S3 and
        # attach to the session is the next step once storage is decided;
        # for now just log where it landed.
        logger.info("srs dvr for %s: %s", session.id, body.get("file"))
        return Response(OK)


# ---------------------------------------------------------------------------
# HLS authorization (Caddy forward_auth)
# ---------------------------------------------------------------------------
#
# Everything above runs because SRS asks. SRS never asks about HLS: the
# playlist and its segments are plain files served by its own http_server
# (port 8080, fronted by Caddy at live.<domain>/live/*), so nothing reached
# _may_play for them. The hls_url that play_credentials hands out carries a
# `?token=`, which made the path *look* protected while the token did
# nothing on it — a viewer the seller banned kept watching, and the app's
# own eight-second WebRTC fallback would even move them onto HLS; a stream
# name kept serving after the session ended; and the URL worked for anyone
# it was passed to.
#
# The proxy is the only thing we control in an HLS request's path, so Caddy
# asks us: `forward_auth` on the /live/* handler turns every playlist and
# segment request into a GET here and serves the file only on a 2xx. Caddy
# rewrites the URI to this endpoint, so the request to judge arrives in
# X-Forwarded-Uri, and the viewer's address in X-Forwarded-For — which the
# Caddyfile overwrites with the real peer, so a client cannot name someone
# else there.
#
# Why segments are judged by address and not by token
# ---------------------------------------------------
# A live m3u8 lists its segments by relative name, and no player copies the
# playlist's query string onto them — not hls.js, not ExoPlayer or AVPlayer
# behind the app's VideoPlayerController. A segment request therefore
# arrives with no token at all and there is nothing to verify. What we do
# know is that the same client was allowed the playlist a second or two
# ago, because a live player reloads it every target duration (~2s here,
# hls_fragment 2). So an allowed *playlist* fetch leaves a short grant for
# (stream, address) and segments ride on it; a segment never mints or
# renews a grant of its own, or a client could stop reloading the playlist
# and never be re-checked again. Behind CGNAT — most Iraqi mobile links —
# that grant is shared with anyone else on the same carrier address
# watching the same stream, which is the price of it. It is not a way back
# in for a banned viewer: their own playlist fetch is refused, so their
# player never learns a segment name to ask for. A segment that does carry
# a token is judged on the token instead, which is better evidence and
# costs three lines to accept if a player ever starts sending it.
#
# What a ban costs now
# --------------------
# Up to HLS_DECISION_TTL_SECONDS of a stale allow, and a playlist fetch
# inside that window leaves a grant good for HLS_GRANT_TTL_SECONDS more:
# about fifteen seconds of authorized requests after the ban, plus the few
# seconds the player has already buffered. Against a path that runs 6+
# seconds behind live anyway, and against today's "never", that is a fair
# trade. It is also more than the WebRTC path does: on_play is asked once
# when play starts, so mid-stream a WHEP viewer is only stopped by the app
# kicking itself on the `viewer_banned` socket event
# (live_viewer_screen._onBanned). An old build, or a hand-rolled player,
# ignores that; it does not get to ignore this.
#
# Token expiry mid-broadcast
# --------------------------
# One token serves a whole watch here — the player reuses the m3u8 URL it
# was given until it stops. At LIVE_TOKEN_TTL_SECONDS (an hour by default)
# a viewer of a six-hour broadcast is refused at the hour mark, and the app
# has no reconnect: _fallBackToHls has already spent its one attempt, so
# the player surfaces an error and the viewer is left on a dead screen
# until they leave the session and come back. Refusing is still right — an
# expired token is not a token, and honouring it would put the only
# unbounded credential in the system on the one path with no hooks. The
# answer is the TTL, not an exception: set LIVE_TOKEN_TTL_SECONDS to cover
# the longest broadcast you allow (21600 for six hours) and the case never
# arises. That is cheap because the token's life is not the security
# boundary on this path — _hls_allowed is, and it re-reads the ban and the
# session's status every few seconds, so a long-lived token still stops
# working seconds after a ban or the end of the session. deploy/README.md
# says the same thing where the value is set.
#
# Cost of asking
# --------------
# One request here per playlist reload and per segment — about one per
# viewer per second, so around a hundred a second at a hundred viewers, on
# a box whose README already names bandwidth as the ceiling. Hence the
# caches below: a segment is one cache read and no query at all, and a
# playlist costs one ban query per viewer every few seconds rather than one
# per fetch.

# How stale an allow or a deny for one (stream, viewer) may be. Well under
# the time a ban stays useful, and far longer than the ~2s between a
# player's playlist reloads, which is the point.
HLS_DECISION_TTL_SECONDS = 5

# How long segments keep flowing on the grant left by the last allowed
# playlist fetch from that address. Must outlast the reload interval with
# room for one lost request, or an ordinary viewer stutters.
HLS_GRANT_TTL_SECONDS = 10


def _hls_target(uri):
    """`(stream_name, is_playlist)` for an HLS request, else `(None, False)`.

    SRS names its files `[app]/[stream].m3u8` and `[app]/[stream]-[seq].ts`
    (infra/live/srs.conf), and a stream is `live_<16 hex>` with no dash in
    it, so the trailing `-<digits>` is unambiguous. Anything else under
    /live/ — a DVR .mp4 above all — is not something this endpoint lets
    through, which is the right default while nothing uploads or deletes
    those recordings.
    """
    name = urlsplit(uri or "").path.rsplit("/", 1)[-1]
    if name.endswith(".m3u8"):
        return name[:-len(".m3u8")] or None, True
    if name.endswith(".ts"):
        stem = name[:-len(".ts")]
        base, dash, seq = stem.rpartition("-")
        return (base if dash and seq.isdigit() else stem) or None, False
    return None, False


def _hls_grant_key(stream, address):
    return "live:hls:grant:%s:%s" % (stream, address)


def _hls_allowed(stream, user_id):
    """`_may_play` for the HLS path, cached for a few seconds.

    The cache sits above the session lookup, not just the ban query: both
    are database work and a segment request must do neither. A deny is
    cached as well as an allow — otherwise a refused viewer whose player
    keeps retrying is the cheapest query generator in the system. Only
    booleans are ever stored, so a `None` back from the cache is
    unambiguously a miss.
    """
    key = "live:hls:ok:%s:%d" % (stream, user_id)
    allowed = cache.get(key)
    if allowed is None:
        session = LiveSession.objects.filter(stream_name=stream).first()
        allowed = bool(session and _may_play(session, user_id))
        cache.set(key, allowed, HLS_DECISION_TTL_SECONDS)
    return allowed


def _client_address(request):
    """The viewer's address, as Caddy observed it.

    The Caddyfile replaces X-Forwarded-For with the real peer rather than
    appending to it, so the header is ours alone. The rightmost entry is
    read anyway: that is the one a proxy appends and the one a client
    cannot forge, so a header the client sent itself cannot borrow someone
    else's grant.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.rsplit(",", 1)[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


class HlsAuthView(APIView):
    """Caddy's forward_auth target for live.<domain>/live/* — 204 or 403.

    No body either way: it would be rendered once per segment, and Caddy
    copies a refusal's body straight to a player that can do nothing with
    it.
    """
    authentication_classes = []
    permission_classes = [AllowAny]
    # Deliberately unthrottled. Every request here carries Caddy's own
    # address and there is about one per viewer per second, so a per-IP
    # rate introduced later for the rest of the API would refuse the whole
    # HLS path at once instead of one abuser. The secret below is the gate.
    throttle_classes = []

    def get(self, request):
        if request.query_params.get("secret", "") != getattr(settings, "SRS_HOOK_SECRET", None):
            # Same shared secret as the SRS hooks: proves the caller is our
            # Caddy, and not a stranger using this as an oracle for who is
            # banned from which session.
            return Response(status=403)

        # forward_auth always subrequests with GET and reports the real verb
        # here. /live/* is files; nothing else has any business there.
        if request.headers.get("X-Forwarded-Method", "GET").upper() not in ("GET", "HEAD"):
            return Response(status=403)

        uri = request.headers.get("X-Forwarded-Uri", "")
        stream, is_playlist = _hls_target(uri)
        if not stream:
            return Response(status=403)

        token = (parse_qs(urlsplit(uri).query).get("token") or [""])[0]
        address = _client_address(request)

        if token:
            user_id = verify_stream_token(token, stream_name=stream, role=ROLE_SUBSCRIBER)
            if not user_id or not _hls_allowed(stream, user_id):
                return Response(status=403)
            if is_playlist and address:
                cache.set(_hls_grant_key(stream, address), True, HLS_GRANT_TTL_SECONDS)
            return Response(status=204)

        # No token. Only a segment can legitimately be in that position —
        # a playlist request is the app's own and always carries one.
        if is_playlist or not address:
            return Response(status=403)
        if not cache.get(_hls_grant_key(stream, address)):
            return Response(status=403)
        return Response(status=204)
