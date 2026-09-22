from django.conf import settings

from .base import MediaProviderError


def get_live_media_provider():
    """The provider named by LIVE_MEDIA_PROVIDER ('srs' or 'agora')."""
    name = (getattr(settings, "LIVE_MEDIA_PROVIDER", "srs") or "srs").lower()
    if name == "srs":
        from .srs import SrsProvider
        return SrsProvider()
    raise MediaProviderError("unknown LIVE_MEDIA_PROVIDER %r" % name)
