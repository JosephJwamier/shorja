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
def plain_static_storage(settings):
    """Production hashes and compresses static files
    (whitenoise.CompressedManifestStaticFilesStorage), which requires a
    manifest built by `collectstatic`. Tests never run collectstatic, so
    every template that resolves a {% static %} path — i.e. every admin
    page — would raise "Missing staticfiles manifest entry". Hashing is a
    delivery optimisation with nothing to assert, so tests use the plain
    storage and production keeps the real one."""
    settings.STORAGES = {
        **settings.STORAGES,
        'staticfiles': {
            'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'
        },
    }


@pytest.fixture(autouse=True)
def toy_prices_allowed(settings):
    """Most tests sell at prices like 100: the floors under prices and under
    trusted sales have tests of their own that set the real values."""
    settings.MIN_ITEM_PRICE = 0
    settings.MIN_TRUSTED_SALE = 0


@pytest.fixture(autouse=True)
def no_request_ceiling(settings):
    """Every test client is one address; a test about the ceiling sets its own."""
    settings.REQUEST_RATES = {k: '1000000/min' for k in settings.REQUEST_RATES}


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
