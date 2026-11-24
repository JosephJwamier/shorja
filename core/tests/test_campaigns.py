"""صفحات الكتيّبات المطبوعة (core/campaign_views.py).

الورق لا يُعدَّل بعد الطباعة: رمز مطبوع يجب ألا يقود إلى 404 أبداً، وما
يُقاس عليه قرار «أيّ كتيّب ينجح» يجب أن يكون قرّاءً لا جالبي معاينات.
"""
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse

from core.models import Campaign, CampaignEvent, User

pytestmark = pytest.mark.django_db

ANDROID = 'Mozilla/5.0 (Linux; Android 14; SM-S911B) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36'
IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1'
PLAY = 'https://play.google.com/store/apps/details?id=com.balla.balla'


def visit(client, path, agent=ANDROID):
    return client.get(path, HTTP_USER_AGENT=agent)


class TestLanding:
    @pytest.mark.parametrize('code', ['c1', 'c2', 'c3', 'm1', 'm2', 'm3'])
    def test_every_printed_code_opens(self, client, code):
        assert visit(client, f'/f/{code}').status_code == 200

    @pytest.mark.parametrize('path', ['/f/zzz', '/f/zzz/book', '/f/zzz/get/play'])
    def test_an_unknown_code_is_never_a_404(self, client, path):
        response = visit(client, path)
        assert response.status_code == 302 and response.url == '/get'

    def test_a_capital_letter_still_opens(self, client):
        assert visit(client, '/f/C1').status_code == 200

    def test_the_download_place_is_visible_before_publishing(self, client):
        html = visit(client, '/f/c1').content.decode()
        assert 'حمّل التطبيق من هنا' in html
        assert html.count('قريباً هنا') == 2
        assert '/get/play' not in html

    def test_a_link_set_later_becomes_a_button(self, client, settings):
        settings.APK_DOWNLOAD_URL = 'https://media.shorjeh.com/shorjeh.apk'
        html = visit(client, '/f/c1').content.decode()
        assert '/f/c1/get/apk' in html
        assert 'أندرويد — قريباً' not in html

    def test_the_readers_own_platform_comes_first(self, client, settings):
        settings.PLAY_STORE_URL = PLAY
        settings.APP_STORE_URL = 'https://apps.apple.com/iq/app/id1'
        html = visit(client, '/f/c1', IPHONE).content.decode()
        assert html.index('/get/ios') < html.index('/get/play')
        html = visit(client, '/f/c1', ANDROID).content.decode()
        assert html.index('/get/play') < html.index('/get/ios')

    def test_no_script_may_run_on_it(self, client):
        assert 'script-src' not in visit(client, '/f/c1')['Content-Security-Policy']

    def test_a_campaign_with_a_wrong_image_name_still_opens(self, client):
        Campaign.objects.filter(code='c1').update(image_name='nope')
        assert visit(client, '/f/c1').status_code == 200


class TestMeasuring:
    def test_the_same_reader_counts_once(self, client):
        visit(client, '/f/c1')
        visit(client, '/f/c1')
        events = CampaignEvent.objects.filter(campaign__code='c1')
        assert events.count() == 2
        assert events.filter(is_new=True).count() == 1

    def test_booklets_are_counted_apart(self, client):
        visit(client, '/f/c1')
        visit(client, '/f/m1')
        assert CampaignEvent.objects.filter(campaign__code='m1', is_new=True).count() == 1

    @pytest.mark.parametrize('agent', [
        'WhatsApp/2.23.20 A', 'TelegramBot (like TwitterBot)', 'facebookexternalhit/1.1',
        'Mozilla/5.0 (compatible; Googlebot/2.1)', 'curl/8.4'])
    def test_link_previews_are_not_readers(self, client, agent):
        assert visit(client, '/f/c1', agent).status_code == 200
        assert not CampaignEvent.objects.exists()

    def test_a_visit_from_the_web_book_is_not_a_scan(self, client):
        visit(client, '/f/c1?s=web')
        assert CampaignEvent.objects.get().source == 'web'

    def test_the_download_is_counted_then_redirected_with_its_source(self, client, settings):
        settings.PLAY_STORE_URL = PLAY
        response = visit(client, '/f/m2/get/play')
        query = parse_qs(urlsplit(response.url).query)
        assert query['id'] == ['com.balla.balla']
        assert 'utm_campaign=m2' in query['referrer'][0]
        event = CampaignEvent.objects.get()
        assert (event.kind, event.target, event.platform) == ('click', 'play', 'android')

    @pytest.mark.parametrize('target', ['play', 'ios', 'apk', 'evil'])
    def test_a_missing_link_goes_back_and_counts_nothing(self, client, target, settings):
        settings.PLAY_STORE_URL = settings.APP_STORE_URL = settings.APK_DOWNLOAD_URL = ''
        assert visit(client, f'/f/c1/get/{target}').url == '/f/c1'
        assert not CampaignEvent.objects.exists()


class TestBook:
    @pytest.mark.parametrize('code', ['c1', 'c2', 'c3', 'm1', 'm2', 'm3'])
    def test_it_opens_with_eight_pages(self, client, code):
        html = visit(client, f'/f/{code}/book').content.decode()
        assert html.count('class="page"') == 8

    def test_only_our_own_script_may_run(self, client):
        csp = visit(client, '/f/c1/book')['Content-Security-Policy']
        assert csp.endswith("script-src 'self'")


class TestAdmin:
    def test_the_comparison_shows_visitors_and_clicks(self, client, settings):
        from django.test import RequestFactory
        from django.contrib import admin
        from core.admin import CampaignAdmin
        settings.PLAY_STORE_URL = PLAY
        visit(client, '/f/c1')
        visit(client, '/f/c1')
        visit(client, '/f/c1/get/play')
        Campaign.objects.filter(code='c1').update(printed_copies=50)
        model_admin = CampaignAdmin(Campaign, admin.site)
        row = model_admin.get_queryset(RequestFactory().get('/')).get(code='c1')
        assert model_admin.visitors(row) == 1
        assert model_admin.opens(row) == 2
        assert model_admin.click_rate(row) == '100'
        assert model_admin.per_copy(row) == '2.0'
        assert model_admin.last_week(row) == 1
