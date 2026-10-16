"""صفحات معاينة روابط المشاركة (core/share_views.py).

تُطلب من جالب واتساب أو تيليغرام — لا حساب، لا رمز، ولا جافاسكربت يُنفَّذ.
فما يهمّ هنا شيئان: أن تُفتح بلا مصادقة، وأن تحمل وسوم Open Graph بروابط
مطلقة (الجالب على خادم بعيد، والمسار النسبي لا يعني له شيئاً).
"""
import pytest
from django.contrib.auth import get_user_model

from core.models import (
    AuctionListing, Category, FixedPriceListing, Item, UserStore,
)

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def seller():
    return User.objects.create_user(
        username='seller', password='x', phone='07701234567')


@pytest.fixture
def item(seller):
    category = Category.objects.create(name='Phones', name_ar='هواتف')
    return Item.objects.create(
        owner=seller, title='Galaxy S24', name_ar='غالاكسي إس ٢٤',
        description='هاتف بحالة ممتازة', category=category,
        condition='new', status='active')


@pytest.fixture
def listing(item):
    return FixedPriceListing.objects.create(
        item=item, price=250000, deposit_percentage=10, is_active=True)


class TestOpenToAnyone:
    """الجالب لا يملك حساباً. صفحة تطلب مصادقة لا تُرسم بطاقة أبداً."""

    def test_product_page_needs_no_account(self, client, listing):
        assert client.get(f'/p/{listing.id}').status_code == 200

    def test_store_page_needs_no_account(self, client, seller):
        shop = UserStore.objects.create(user=seller, name='متجر علي', is_active=True)
        assert client.get(f'/s/{shop.id}').status_code == 200

    def test_auction_page_needs_no_account(self, client, item):
        from django.utils import timezone
        from datetime import timedelta
        now = timezone.now()
        auction = AuctionListing.objects.create(
            item=item, starting_price=100000, current_price=120000,
            minimum_increment=5000, bid_deposit=10000,
            start_time=now, end_time=now + timedelta(days=1))
        assert client.get(f'/a/{auction.id}').status_code == 200

    def test_unknown_id_is_404_not_500(self, client):
        assert client.get('/p/999999').status_code == 404

    def test_an_inactive_listing_is_not_shared(self, client, listing):
        # رابط لعرض أُوقف يجب ألا يبقى يعرض سعراً لم يعد قائماً.
        listing.is_active = False
        listing.save(update_fields=['is_active'])
        assert client.get(f'/p/{listing.id}').status_code == 404


class TestOpenGraphTags:
    def test_card_carries_title_description_and_image(self, client, listing):
        html = client.get(f'/p/{listing.id}').content.decode()
        assert 'property="og:title"' in html
        assert 'property="og:description"' in html
        assert 'property="og:image"' in html
        assert 'property="og:url"' in html

    def test_arabic_name_wins_over_the_english_title(self, client, listing):
        # القارئ عراقي؛ title إنجليزي في الغالب ولا يُقرأ.
        html = client.get(f'/p/{listing.id}').content.decode()
        assert 'غالاكسي إس ٢٤' in html

    def test_image_url_is_absolute(self, client, listing):
        # الجالب على خادم واتساب، لا على هاتف القارئ: مسار نسبي لا يُحلّ.
        html = client.get(f'/p/{listing.id}').content.decode()
        start = html.index('property="og:image" content="') + len(
            'property="og:image" content="')
        url = html[start:html.index('"', start)]
        assert url.startswith('http://') or url.startswith('https://'), url

    def test_price_is_shown(self, client, listing):
        html = client.get(f'/p/{listing.id}').content.decode()
        assert '250,000' in html

    def test_auction_card_says_it_is_an_auction(self, client, item):
        from django.utils import timezone
        from datetime import timedelta
        now = timezone.now()
        auction = AuctionListing.objects.create(
            item=item, starting_price=100000, current_price=120000,
            minimum_increment=5000, bid_deposit=10000,
            start_time=now, end_time=now + timedelta(days=1))
        html = client.get(f'/a/{auction.id}').content.decode()
        assert 'مزاد' in html
        assert '120,000' in html


class TestCaching:
    def test_an_auction_is_cached_shorter_than_a_product(self, client, listing, item):
        from django.utils import timezone
        from datetime import timedelta
        now = timezone.now()
        # السعر الحالي يتغيّر مع كل مزايدة؛ بطاقة محفوظة خمس دقائق تعرض
        # رقماً تجاوزه المزاد.
        second = Item.objects.create(
            owner=item.owner, title='T', name_ar='ت', description='d',
            category=item.category, condition='new', status='active')
        auction = AuctionListing.objects.create(
            item=second, starting_price=1000, current_price=1000,
            minimum_increment=100, bid_deposit=100,
            start_time=now, end_time=now + timedelta(days=1))

        product_age = client.get(f'/p/{listing.id}')['Cache-Control']
        auction_age = client.get(f'/a/{auction.id}')['Cache-Control']
        assert 'max-age=300' in product_age
        assert 'max-age=60' in auction_age


class TestHomePage:
    """الجذر كان مسجَّلاً في share_views ومفقوداً من urls.py، فردّ 404 على
    أول من فتح shorjeh.com. دالة موجودة بلا مسار لا يكشفها شيء — لا
    المحلّل ولا `manage.py check` — فيحرسها اختبار."""

    def test_the_site_root_answers(self, client):
        assert client.get('/').status_code == 200

    def test_it_says_what_the_place_is(self, client):
        html = client.get('/').content.decode()
        assert 'الشورجة' in html
        assert 'مزادات' in html or 'زايد' in html

    def test_it_carries_a_card_for_sharing(self, client):
        html = client.get('/').content.decode()
        assert 'property="og:title"' in html
        assert 'property="og:description"' in html

    def test_it_links_the_legal_pages_the_stores_require(self, client):
        html = client.get('/').content.decode()
        assert '/legal/privacy/' in html
        assert '/legal/terms/' in html

    def test_no_dead_download_button_before_the_store_link_exists(self, client):
        # زرّ يقود إلى 404 أسوأ من زرّ غائب.
        html = client.get('/').content.decode()
        assert 'قريباً' in html
