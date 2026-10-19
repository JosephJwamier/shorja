"""Live-stream media providers.

Live video runs on our own SRS box (infra/live/) — WHIP in from the
seller's phone, WHEP out to viewers, HLS as the fallback when WebRTC
cannot get through. Everything vendor-shaped lives behind
`LiveMediaProvider` so the viewsets and the app see one envelope:

  - `LiveSessionViewSet.join_token` asks for publish/play credentials;
  - `core.live_hooks` answers the box's on_publish/on_play callbacks;
  - `settings.LIVE_MEDIA_PROVIDER` picks the implementation.

Nothing here reaches out over the network: credentials are signed
locally (see srs.py), so issuing them cannot fail because the box is
busy — only because it was never configured.
"""
from .registry import get_live_media_provider  # noqa: F401
