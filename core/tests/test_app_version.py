"""الحدّ الأدنى لنسخة التطبيق."""
import pytest
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db
URL = '/api/app/version/'


def test_no_minimum_by_default(settings):
    settings.APP_MIN_BUILD_ANDROID = 0
    body = APIClient().get(URL).json()
    assert body['min_build'] == 0


def test_android_is_the_default_platform(settings):
    settings.APP_MIN_BUILD_ANDROID = 7
    settings.PLAY_STORE_URL = 'https://play.google.com/store/apps/details?id=x'
    body = APIClient().get(URL).json()
    assert body == {'platform': 'android', 'min_build': 7,
                    'store_url': settings.PLAY_STORE_URL}


def test_ios_gets_its_own_floor_and_store(settings):
    settings.APP_MIN_BUILD_ANDROID = 7
    settings.APP_MIN_BUILD_IOS = 3
    settings.APP_STORE_URL = 'https://apps.apple.com/app/id1'
    body = APIClient().get(URL, {'platform': 'ios'}).json()
    assert (body['min_build'], body['store_url']) == (3, settings.APP_STORE_URL)


def test_an_expired_token_does_not_hide_the_answer(settings):
    # نسخة قديمة برمز منتهٍ هي بالضبط من يحتاج هذا الجواب.
    settings.APP_MIN_BUILD_ANDROID = 5
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Bearer expired.or.garbage')
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json()['min_build'] == 5


def test_unknown_platform_falls_back_to_android(settings):
    settings.APP_MIN_BUILD_ANDROID = 4
    assert APIClient().get(URL, {'platform': 'web'}).json()['min_build'] == 4
