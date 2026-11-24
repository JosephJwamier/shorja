"""Live-stream media providers.

Live video runs on our own SRS box (infra/live/) — WHIP in from the
seller's phone, WHEP out to viewers, HLS as the fallback when WebRTC
cannot get through. Everything vendor-shaped lives behind
`LiveMediaProvider` so the viewsets and the app see one envelope:

  - `LiveSessionViewSet.join_token` asks for publish/play credentials;
  - `core.live_hooks` answers the box's on_publish/on_play callbacks;
  - `settings.LIVE_MEDIA_PROVIDER` picks the implementation.

Issuing credentials never reaches out over the network: they are signed
locally (see srs.py), so it cannot fail because the box is busy — only
because it was never configured. `disconnect` does, and never fails its
caller: the ban or the end of the session stands either way.
"""
from .registry import get_live_media_provider  # noqa: F401


def disconnect(session, *, user_id=None):
    """After commit, cut `user_id`'s connections to the session's stream, or
    all of them. A media server that cannot be reached is logged, not raised."""
    import logging

    from django.db import transaction

    def cut():
        try:
            get_live_media_provider().disconnect(session, user_id=user_id)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                'could not disconnect %s from session %s: %s',
                user_id or 'everyone', session.pk, exc)
    transaction.on_commit(cut)
