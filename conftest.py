"""
Project-wide pytest fixtures.

Tests used to share the developer's live Redis cache (settings.CACHES points
at localhost:6379/1 for every environment). That was harmless until the auth
endpoints gained rate limits: DRF keeps throttle counters in the default
cache, keyed by IP / username, and a 500-test run from 127.0.0.1 would trip
them — or, worse, leave the developer's own login throttled after the run.
Every test now gets an empty in-process cache instead.
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_cache(settings):
    settings.CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'pytest',
        }
    }
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()
