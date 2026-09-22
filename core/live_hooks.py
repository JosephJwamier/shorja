"""SRS → Django callbacks (http_hooks in infra/live/srs.conf).

NOT ROUTED YET. When the live box goes up, add to core/urls.py:

    path('live/srs/<str:event>/', live_hooks.SrsHookView.as_view(), name='srs-hook'),

SRS POSTs a JSON body like
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
"""
import logging
from urllib.parse import parse_qs

from django.conf import settings
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

    `user_id` is 0 for a token minted for an anonymous caller. Bans live in
    LiveSessionBan (session.bans, user_id). Return True to allow.
    """
    # TODO(live-media, still undecided): decide the viewing policy. Options, from most open to
    # most closed:
    #   (a) anyone with a valid play token watches, bans enforced;
    #   (b) same, but anonymous tokens (user_id == 0) are refused so every
    #       viewer is an account — needed if you want viewer counts,
    #       moderation and "who bought during the stream" to be reliable;
    #   (c) (b) plus the session must be 'live' — refuse replays of a
    #       stream name after the session ended.
    raise NotImplementedError


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

        session = LiveSession.objects.filter(agora_channel_name=stream).first()
        if session is None:
            logger.warning("srs %s for unknown stream %r", event, stream)
            return Response(DENY)
        return handler(session, token, body)

    # -- publishing ------------------------------------------------------
    def handle_on_publish(self, session, token, body):
        user_id = verify_stream_token(token, stream_name=session.agora_channel_name,
                                      role=ROLE_PUBLISHER)
        if user_id is None:
            logger.warning("srs on_publish denied for %s (bad token)", session.id)
            return Response(DENY)
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
        user_id = verify_stream_token(token, stream_name=session.agora_channel_name,
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
