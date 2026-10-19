from django.conf import settings

from .base import MediaProviderError


def get_live_media_provider():
    """The provider named by LIVE_MEDIA_PROVIDER.

    Only 'srs' exists. An unknown name raises rather than falling back to
    a default: a typo in production must stop live media loudly, not
    quietly hand clients credentials for a server nobody meant to use.
    """
    name = (getattr(settings, "LIVE_MEDIA_PROVIDER", "srs") or "srs").lower()
    if name == "srs":
        from .srs import SrsProvider
        return SrsProvider()
    raise MediaProviderError("unknown LIVE_MEDIA_PROVIDER %r" % name)
