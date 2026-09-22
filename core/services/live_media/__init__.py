"""SRS provider scaffolding - NOT wired in yet.

The app and the join-token endpoint still run on Agora (core/agora_utils.py).
When the Hetzner box is up and infra/live/ is deployed, switching means:
  1. settings: LIVE_MEDIA_PROVIDER='srs', SRS_PUBLIC_BASE_URL, LIVE_TOKEN_SECRET,
     SRS_HOOK_SECRET, TURN_HOST/TURN_SECRET (see infra/live/README.md);
  2. LiveSessionViewSet.join_token -> get_live_media_provider().publish_/play_credentials();
  3. route core/live_hooks.py (SRS on_publish/on_play callbacks);
  4. the Flutter media layer (WHIP/WHEP via flutter_webrtc).
Nothing here is imported by the running code.
"""
from .registry import get_live_media_provider  # noqa: F401
