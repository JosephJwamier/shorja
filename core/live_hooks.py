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
