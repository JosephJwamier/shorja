"""
Reel object-storage wiring (core/aws_utils.py) after the S3 -> Cloudflare R2
move. Pure unit tests: the boto3 client is built but never talks to the
network — only its construction-time metadata (endpoint, region, config) and
the public-URL builder are asserted. No DB, no HTTP.
"""
from unittest.mock import patch

from django.test import override_settings

from core import aws_utils


def _fresh_client(**settings_overrides):
    """aws_utils caches one client per process; drop it so each test sees
    the settings it overrides rather than whatever the previous test built."""
    aws_utils._s3_client = None
    with override_settings(**settings_overrides):
        return aws_utils.get_s3_client()


def teardown_module(module):
    # Don't leak a test-built client into any later test module.
    aws_utils._s3_client = None


R2_SETTINGS = dict(
    AWS_ACCESS_KEY_ID='r2-key',
    AWS_SECRET_ACCESS_KEY='r2-secret',
    AWS_S3_REGION_NAME='auto',
    AWS_STORAGE_BUCKET_NAME='shorja-reels',
    S3_ENDPOINT_URL='https://0123456789abcdef.r2.cloudflarestorage.com',
)


class TestClientConstruction:
    def test_r2_endpoint_and_region_are_used_when_endpoint_set(self):
        client = _fresh_client(**R2_SETTINGS)
        assert client.meta.endpoint_url == R2_SETTINGS['S3_ENDPOINT_URL']
        assert client.meta.region_name == 'auto'

    def test_empty_endpoint_falls_back_to_aws_regional_endpoint(self):
        client = _fresh_client(**{**R2_SETTINGS,
                                  'AWS_S3_REGION_NAME': 'eu-north-1',
                                  'S3_ENDPOINT_URL': ''})
        # boto3's own default for a real AWS region — proves the endpoint
        # kwarg is genuinely omitted rather than passed as ''.
        assert client.meta.endpoint_url == 'https://s3.eu-north-1.amazonaws.com'

    def test_checksum_behaviour_is_when_required_for_r2(self):
        client = _fresh_client(**R2_SETTINGS)
        cfg = client.meta.config
        assert cfg.request_checksum_calculation == 'when_required'
        assert cfg.response_checksum_validation == 'when_required'
        assert cfg.signature_version == 's3v4'

    def test_client_is_cached_per_process(self):
        first = _fresh_client(**R2_SETTINGS)
        with override_settings(**R2_SETTINGS):
            assert aws_utils.get_s3_client() is first

    def test_presigned_put_targets_r2_endpoint_and_signs_content_type(self):
        _fresh_client(**R2_SETTINGS)
        with override_settings(**R2_SETTINGS, REEL_PRESIGNED_URL_TTL_SECONDS=300):
            url = aws_utils.build_presigned_put_url('incoming/7/abc/video.mp4', 'video/mp4')
        assert url.startswith(R2_SETTINGS['S3_ENDPOINT_URL'] + '/shorja-reels/incoming/7/abc/video.mp4?')
        # The app must PUT with this exact Content-Type — it's part of the signature.
        assert 'X-Amz-SignedHeaders=content-type%3Bhost' in url
        assert 'X-Amz-Expires=300' in url


class TestObjectUrl:
    @override_settings(REELS_PUBLIC_BASE_URL='https://media.example.com')
    def test_builds_public_url_from_custom_domain(self):
        assert aws_utils.object_url('reels/7/abc/video.mp4') == \
            'https://media.example.com/reels/7/abc/video.mp4'

    @override_settings(REELS_PUBLIC_BASE_URL='https://media.example.com/')
    def test_trailing_slash_on_base_does_not_double_the_slash(self):
        assert aws_utils.object_url('reels/7/abc/thumb.jpg') == \
            'https://media.example.com/reels/7/abc/thumb.jpg'

    @override_settings(REELS_PUBLIC_BASE_URL='https://media.example.com')
    def test_empty_key_gives_none_not_a_bare_domain(self):
        # A reel without a thumbnail has thumbnail_key='' — the API must
        # return null, not the CDN root as if it were an image.
        assert aws_utils.object_url('') is None
        assert aws_utils.object_url(None) is None


class TestBunnyFallbackInSettings:
    """settings.REELS_PUBLIC_BASE_URL is resolved at import time from either
    REELS_PUBLIC_BASE_URL or the legacy BUNNYCDN_PULL_ZONE_BASE_URL env var.
    Re-evaluate that one expression under controlled env vars rather than
    re-importing the whole settings module."""

    @staticmethod
    def _resolve(env):
        import os
        with patch.dict(os.environ, env, clear=False):
            return (
                os.environ.get('REELS_PUBLIC_BASE_URL')
                or os.environ.get('BUNNYCDN_PULL_ZONE_BASE_URL', '')
            )

    def test_new_var_wins_when_both_set(self):
        assert self._resolve({
            'REELS_PUBLIC_BASE_URL': 'https://media.example.com',
            'BUNNYCDN_PULL_ZONE_BASE_URL': 'https://old.b-cdn.net',
        }) == 'https://media.example.com'

    def test_empty_new_var_falls_back_to_bunny(self):
        # .env ships REELS_PUBLIC_BASE_URL= (empty) until the R2 domain is
        # connected — the old Bunny zone must keep serving in the meantime.
        assert self._resolve({
            'REELS_PUBLIC_BASE_URL': '',
            'BUNNYCDN_PULL_ZONE_BASE_URL': 'https://old.b-cdn.net',
        }) == 'https://old.b-cdn.net'


class TestProductionGuard:
    """settings._check_reels_storage_config() runs at import time when
    DEBUG=False. Loading balla.settings fresh under a controlled environment
    is the only honest way to test it — it's module-level code, and the
    variables it reads are module globals."""

    BASE_ENV = {
        'DEBUG': 'False',
        'DJANGO_SECRET_KEY': 'unit-test-secret-not-the-placeholder',
        'ALLOWED_HOSTS': 'api.example.com',
        'AWS_STORAGE_BUCKET_NAME': 'shorja-reels',
        'S3_ENDPOINT_URL': 'https://0123456789abcdef.r2.cloudflarestorage.com',
        'REELS_PUBLIC_BASE_URL': 'https://media.example.com',
        'BUNNYCDN_PULL_ZONE_BASE_URL': '',
    }

    @staticmethod
    def _load_settings(env):
        import importlib.util
        import os
        import sys
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / 'balla' / 'settings.py'
        # Keep .env's local values (DEBUG=True etc.) from masking the test
        # env: settings' _load_env_file uses setdefault, so anything set here
        # wins, and clear=True drops the real process values too.
        with patch.dict(os.environ, env, clear=True):
            spec = importlib.util.spec_from_file_location('_settings_under_test', path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(spec.name, None)
            return module

    def _assert_rejected(self, env_overrides, expected_fragment):
        import pytest
        from django.core.exceptions import ImproperlyConfigured

        with pytest.raises(ImproperlyConfigured) as excinfo:
            self._load_settings({**self.BASE_ENV, **env_overrides})
        assert expected_fragment in str(excinfo.value)

    def test_sane_production_config_boots(self):
        module = self._load_settings(self.BASE_ENV)
        assert module.DEBUG is False
        assert module.REELS_PUBLIC_BASE_URL == 'https://media.example.com'

    def test_empty_public_base_is_rejected(self):
        self._assert_rejected({'REELS_PUBLIC_BASE_URL': ''}, 'REELS_PUBLIC_BASE_URL must be set')

    def test_http_public_base_is_rejected(self):
        self._assert_rejected({'REELS_PUBLIC_BASE_URL': 'http://media.example.com'},
                              'must start with https://')

    def test_r2_dev_subdomain_is_rejected(self):
        self._assert_rejected({'REELS_PUBLIC_BASE_URL': 'https://pub-abc123.r2.dev'},
                              'r2.dev')

    def test_empty_bucket_is_rejected(self):
        self._assert_rejected({'AWS_STORAGE_BUCKET_NAME': ''}, 'AWS_STORAGE_BUCKET_NAME')

    def test_http_endpoint_is_rejected(self):
        self._assert_rejected({'S3_ENDPOINT_URL': 'http://0123456789abcdef.r2.cloudflarestorage.com'},
                              'S3_ENDPOINT_URL must be an https://')

    def test_empty_endpoint_means_aws_and_is_allowed(self):
        # By design: no endpoint = plain AWS S3, a valid backend, not a
        # half-migrated one.
        module = self._load_settings({**self.BASE_ENV, 'S3_ENDPOINT_URL': ''})
        assert module.S3_ENDPOINT_URL == ''

    def test_guard_is_skipped_in_debug(self):
        module = self._load_settings({**self.BASE_ENV, 'DEBUG': 'True',
                                      'REELS_PUBLIC_BASE_URL': ''})
        assert module.DEBUG is True
