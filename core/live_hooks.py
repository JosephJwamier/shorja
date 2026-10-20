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
served by SRS's own http_server, and no hook fires for them. Django writes
the playlist itself and signs the segment URIs in it instead — see
HlsPlaylistView and HlsAuthView at the bottom of this file.
"""
import logging
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import LiveSession
from .services.live_media.base import MediaProviderError
from .services.live_media.srs import (
    ROLE_PUBLISHER, ROLE_SUBSCRIBER, make_stream_token, verify_stream_token,
)

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
        expected = getattr(settings, "SRS_HOOK_SECRET", "") or ""
        if not expected:
            # SRS_HOOK_SECRET defaults to '' (settings.py), so an unset env
            # var used to make this check compare "" against "" and pass —
            # layer 1 of the two documented above silently became a no-op on
            # any deploy that forgot the variable, and that is every fresh
            # one, since .env.production.example ships it blank. Fail closed:
            # an unconfigured secret is not a matching secret.
            logger.error("SRS hook called but SRS_HOOK_SECRET is not set")
            return Response(DENY, status=403)
        # compare_digest, not !=: the secret is a fixed string checked on
        # every publish and play, which is exactly the shape a timing
        # comparison leaks.
        if not secrets.compare_digest(
                request.query_params.get("secret", ""), expected):
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
# HLS authorization (Caddy: the playlist from Django, segments from SRS)
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
# A segment request has to carry the viewer's credential, and the hard part
# is that nothing in the pipeline puts one there by itself: a live m3u8
# lists its segments by relative name, so the playlist's query string is
# not copied onto them. The answer is the one tokenized HLS normally uses —
# **put the credential in the segment URLs**, which means the playlist has
# to be written rather than served:
#
#   * `/live/<stream>.m3u8` → HlsPlaylistView. It applies the policy, fetches
#     the real playlist from SRS over the loopback, and appends a short-lived
#     signed token to every URI in it.
#   * `/live/<stream>-<seq>.ts` → straight to SRS behind `forward_auth` →
#     HlsAuthView, which reads that token out of the query string.
#
# Caddy splits the two by extension (see the `handle /live/*` block in
# deploy/Caddyfile), and anything else under /live/ — a DVR .mp4 above all —
# is answered 404 there rather than reaching either view.
#
# Three mechanisms that look like answers and are not
# ---------------------------------------------------
# * **A custom request header**, which the app sends and the player repeats.
#   Correct on Android, where video_player hands httpHeaders to
#   `DefaultHttpDataSource.Factory.setDefaultRequestProperties` and that
#   factory backs the whole HLS pipeline. Not dependable on iOS: the headers
#   go on the asset through AVURLAssetHTTPHeaderFieldsKey, an undocumented
#   key that Apple's own engineers call unsupported, and the reports of
#   whether AVFoundation repeats them on segment fetches disagree. Shipped
#   briefly and reverted — a fallback path that is the *only* path for
#   viewers whose network blocks WebRTC must not rest on behaviour that
#   varies by OS version.
#
# * **A cookie** set on the allowed playlist response. The textbook shape,
#   and dead on arrival here: ExoPlayer's DefaultHttpDataSource is
#   HttpURLConnection-based and honours cookies only with a CookieHandler
#   installed as the JVM default, which neither video_player_android nor
#   Android itself does. Every Android segment would arrive bare.
#
# * **The client's address**, remembered from its last allowed playlist
#   fetch. coturn exists in this project precisely because most Iraqi mobile
#   lines are behind CGNAT, so a public address is normally *several*
#   viewers. A banned viewer's playlist fetch is refused, but segment names
#   are `<stream>-<seq>.ts` with a sequential seq: they could extrapolate
#   from the last playlist they were served and keep fetching on a grant
#   that a neighbour on the same address kept alive. Indefinitely.
#
# What a ban costs now
# --------------------
# The ban takes effect at the viewer's next playlist reload, which a live
# player does every target duration (~2s here, hls_fragment 2), delayed by
# up to HLS_DECISION_TTL_SECONDS of a stale decision. After that they get no
# new segment tokens, so they can finish the segments the last allowed
# playlist named — at most one hls_window, 20s, most of which their player
# had already buffered — and nothing newer. The bound is per viewer and
# holds whoever else shares their address. It is also more than the WebRTC
# path does: on_play is asked once when play starts, so mid-stream a WHEP
# viewer is only stopped by the app kicking itself on the `viewer_banned`
# socket event (live_viewer_screen._onBanned). An old build, or a
# hand-rolled player, ignores that; it does not get to ignore this.
#
# Token expiry mid-broadcast
# --------------------------
# The *playlist* token is the one from play_credentials and serves a whole
# watch, because the player keeps re-requesting the same m3u8 URL. At
# LIVE_TOKEN_TTL_SECONDS (an hour by default) a viewer of a six-hour
# broadcast is refused at the hour mark, and the app has no reconnect:
# _fallBackToHls has already spent its one attempt, so the player surfaces
# an error and the viewer is left on a dead screen until they leave the
# session and come back. Refusing is still right — an expired token is not
# a token. The answer is the TTL, not an exception: set
# LIVE_TOKEN_TTL_SECONDS to cover the longest broadcast you allow (21600
# for six hours) and the case never arises. That is cheap because the
# token's life is not the security boundary here — _hls_allowed is, and it
# re-reads the ban and the session's status every few seconds, so a
# long-lived token still stops working seconds after a ban or the end of
# the session. deploy/README.md says the same thing where the value is set.
#
# Cost of asking
# --------------
# A segment is one cache read in HlsAuthView and no query. A playlist is a
# Django request that also fetches a few hundred bytes from SRS; at a
# reload every ~2s per viewer that is 50 fetches a second at a hundred
# viewers, so the upstream body is cached for HLS_ORIGIN_CACHE_SECONDS and
# shared between everyone watching that stream — the signing is per viewer,
# the fetch is not.

# How stale an allow or a deny for one (stream, viewer) may be. Well under
# the time a ban stays useful, and far longer than the ~2s between a
# player's playlist reloads, which is the point.
HLS_DECISION_TTL_SECONDS = 5

# Lifetime of the token written into a playlist's segment lines. It has to
# outlive the window that playlist lists (hls_window 20) plus a reload, or
# an honest viewer's oldest segment is refused mid-watch; beyond that it is
# how long a segment URL survives being passed on.
HLS_SEGMENT_TOKEN_TTL_SECONDS = 30

# How long one fetch of a stream's playlist from SRS is reused across the
# viewers watching it. Half a fragment: shorter than the playlist changes,
# so nobody is served a stale live edge.
HLS_ORIGIN_CACHE_SECONDS = 1

# SRS answers HTTP in well under a second on the loopback; a hung fetch
# here would hold a request thread per viewer per reload.
HLS_ORIGIN_TIMEOUT_SECONDS = 2

# Stream names are `live_<16 hex>`, and the token the caller presents has to
# name the same stream, so a hostile name cannot be signed. This is the
# second lock: the name goes into a URL path we build, and `..` or an
# encoded slash in it would be a traversal on SRS's file server.
_SAFE_STREAM = re.compile(r"^[A-Za-z0-9_-]+$")

# A URI inside a playlist tag — an fMP4 init segment, an encryption key.
# None appear with the current srs.conf (TS segments, hls_keys off), but one
# would be fetched exactly like a segment and refused if left unsigned,
# which is a black screen with nothing to read in any log.
_URI_ATTRIBUTE = re.compile(r'(URI=")([^"]+)(")')


def _hls_stream(uri, *, playlist=None):
    """The stream an HLS request is for, or None if it is not one.

    SRS names its files `[app]/[stream].m3u8` and `[app]/[stream]-[seq].ts`
    (infra/live/srs.conf), and a stream is `live_<16 hex>` with no dash in
    it, so the trailing `-<digits>` is unambiguous. `playlist=True` or
    `False` requires that kind; None accepts either. Anything else under
    /live/ is refused here as well as at the proxy.
    """
    name = urlsplit(uri or "").path.rsplit("/", 1)[-1]
    if name.endswith(".m3u8"):
        stream, is_playlist = name[:-len(".m3u8")], True
    elif name.endswith(".ts"):
        stem = name[:-len(".ts")]
        base, dash, seq = stem.rpartition("-")
        stream, is_playlist = (base if dash and seq.isdigit() else stem), False
    else:
        return None
    if playlist is not None and is_playlist is not playlist:
        return None
    return stream if _SAFE_STREAM.match(stream or "") else None


def _hls_token(uri):
    return (parse_qs(urlsplit(uri or "").query).get("token") or [""])[0]


def _hls_allowed(stream, user_id):
    """`_may_play` for the HLS path, cached for a few seconds.

    The cache sits above the session lookup, not just the ban query: both
    are database work, and at a request per viewer per second neither can
    run every time. A deny is cached as well as an allow — otherwise a
    refused viewer whose player keeps retrying is the cheapest query
    generator in the system. Only booleans are ever stored, so a `None`
    back from the cache is unambiguously a miss.
    """
    key = "live:hls:ok:%s:%d" % (stream, user_id)
    allowed = cache.get(key)
    if allowed is None:
        session = LiveSession.objects.filter(stream_name=stream).first()
        allowed = bool(session and _may_play(session, user_id))
        cache.set(key, allowed, HLS_DECISION_TTL_SECONDS)
    return allowed


def _hls_viewer(uri, *, playlist):
    """`(stream, user_id)` for an authorized HLS request, else `(None, None)`.

    The same three questions for the playlist and for a segment: is this an
    HLS path, is the token a subscriber token for *this* stream, and may
    that viewer watch right now.
    """
    stream = _hls_stream(uri, playlist=playlist)
    if not stream:
        return None, None
    user_id = verify_stream_token(_hls_token(uri), stream_name=stream,
                                  role=ROLE_SUBSCRIBER)
    if not user_id or not _hls_allowed(stream, user_id):
        return None, None
    return stream, user_id


def sign_playlist(body, stream, user_id, *, now=None):
    """A playlist with every URI in it carrying this viewer's token.

    One token for the whole response rather than one per line: a per-line
    token would name the same stream, the same viewer and the same expiry,
    so it would be the same string.
    """
    token = make_stream_token(stream, ROLE_SUBSCRIBER, HLS_SEGMENT_TOKEN_TTL_SECONDS,
                              user_id=user_id, now=now)

    def with_token(uri):
        return "%s%stoken=%s" % (uri, "&" if "?" in uri else "?", token)

    out = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            out.append(_URI_ATTRIBUTE.sub(
                lambda m: m.group(1) + with_token(m.group(2)) + m.group(3), line))
        else:
            out.append(with_token(stripped))
    return "\n".join(out) + "\n"


def fetch_origin_playlist(stream):
    """The raw playlist from SRS, or None if it has none for this stream.

    Cached briefly and shared between viewers — see
    HLS_ORIGIN_CACHE_SECONDS. Raises MediaProviderError when SRS cannot be
    reached at all, which is a different answer from "no such stream".
    """
    key = "live:hls:m3u8:%s" % stream
    body = cache.get(key)
    if body is not None:
        return body or None          # "" is a cached 404
    url = "%s/%s/%s.m3u8" % (
        (getattr(settings, "SRS_HLS_ORIGIN", "") or "http://127.0.0.1:8080").rstrip("/"),
        getattr(settings, "SRS_APP", "live"), stream,
    )
    try:
        response = requests.get(url, timeout=HLS_ORIGIN_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.error("hls origin unreachable for %s: %s", stream, exc)
        raise MediaProviderError("HLS origin unreachable")
    if response.status_code == 404:
        # Nothing is publishing yet, or SRS has disposed of the stream.
        cache.set(key, "", HLS_ORIGIN_CACHE_SECONDS)
        return None
    if response.status_code != 200:
        logger.error("hls origin %s for %s", response.status_code, stream)
        raise MediaProviderError("HLS origin error")
    cache.set(key, response.text, HLS_ORIGIN_CACHE_SECONDS)
    return response.text


class HlsSecretMixin:
    """Both HLS views are reached only through Caddy, which proves it with
    the same shared secret the SRS hooks use. Without it they are an oracle
    for who is banned from which session, answerable by anyone who finds
    the URL."""
    authentication_classes = []
    permission_classes = [AllowAny]
    # Deliberately unthrottled. Every request arrives from Caddy's own
    # address and there is about one per viewer per second, so a per-IP rate
    # introduced later for the rest of the API would refuse the whole HLS
    # path at once instead of one abuser. The secret is the gate.
    throttle_classes = []

    def caddy_sent_this(self, request):
        return request.query_params.get("secret", "") == getattr(settings, "SRS_HOOK_SECRET", None)


class HlsAuthView(HlsSecretMixin, APIView):
    """Caddy's forward_auth target for the segments — 204 or 403.

    No body either way: it would be rendered once per segment, and Caddy
    copies a refusal's body straight to a player that can do nothing with
    it.
    """

    def get(self, request):
        if not self.caddy_sent_this(request):
            return Response(status=403)
        # forward_auth always subrequests with GET and reports the real verb
        # here. /live/* is files; nothing else has any business there.
        if request.headers.get("X-Forwarded-Method", "GET").upper() not in ("GET", "HEAD"):
            return Response(status=403)
        stream, _ = _hls_viewer(request.headers.get("X-Forwarded-Uri", ""), playlist=False)
        return Response(status=204 if stream else 403)


class HlsPlaylistView(HlsSecretMixin, APIView):
    """`live.<domain>/live/<stream>.m3u8`, rewritten for one viewer.

    Caddy sends the original URI in X-Forwarded-Uri, since it rewrites the
    path to reach this view.
    """

    def get(self, request):
        if not self.caddy_sent_this(request):
            return Response(status=403)
        uri = request.headers.get("X-Forwarded-Uri", "")
        stream, user_id = _hls_viewer(uri, playlist=True)
        if not stream:
            return Response(status=403)
        try:
            body = fetch_origin_playlist(stream)
        except MediaProviderError:
            # SRS is down or unreachable. 502 rather than 403: the viewer is
            # entitled to watch, and a refusal here would read as a ban.
            return Response(status=502)
        if body is None:
            return Response(status=404)
        return HttpResponse(sign_playlist(body, stream, user_id),
                            content_type="application/vnd.apple.mpegurl")
