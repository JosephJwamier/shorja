"""Guest browsing: a visitor with no account can look, but cannot act.

Opening read endpoints is the easy half. The half that breaks in production
is serializers that assume request.user exists — AnonymousUser is truthy, so
a `if request.user:` guard passes and the next attribute access raises. These
tests exercise each open route with a genuinely anonymous client so that
failure shows up here rather than on a stranger's phone.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from core.models import Category, Item

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def guest():
    """No credentials at all — not a logged-out user, an unknown visitor."""
    return APIClient()


@pytest.fixture
def seller():
    return User.objects.create_user(
        username='seller', password='x', phone='07701234567')


@pytest.fixture
def item(seller):
    category = Category.objects.create(name='Phones', name_ar='هواتف')
    return Item.objects.create(
        owner=seller, title='Galaxy', description='d',
        category=category, condition='new', status='available')


@pytest.fixture
def listing(item):
    """عرض سعر ثابت ظاهر فعلاً في قوائم التصفّح.

    حالة المنتج تُضبط إلى `active` صريحاً: مِثبت `item` أعلاه يستعمل
    `'available'`، وهي ليست من `Item.STATUS_CHOICES` أصلاً (القيم
    draft/active/reserved/sold/transferred/archived) — و`choices` تُتحقَّق
    في بايثون لا في MySQL فتُخزَّن كما هي. و`FixedPriceListingViewSet`
    يُرشّح على `item__status__in=['active','reserved']`، فعرضٌ على منتج
    بحالة غير صالحة لا يظهر في أي قائمة.
    """
    from decimal import Decimal
    from core.models import FixedPriceListing
    item.status = 'active'
    item.save(update_fields=['status'])
    return FixedPriceListing.objects.create(
        item=item, price=Decimal('250000'), deposit_percentage=Decimal('10'),
        is_active=True)


class TestGuestCanLook:
    def test_item_list(self, guest, item):
        r = guest.get('/api/items/')
        assert r.status_code == 200, r.data

    def test_item_detail(self, guest, item):
        r = guest.get(f'/api/items/{item.id}/')
        assert r.status_code == 200, r.data

    def test_item_detail_reports_not_favorited_rather_than_crashing(self, guest, item):
        # get_is_favorited() must short-circuit on AnonymousUser instead of
        # querying favorited_by with it.
        r = guest.get(f'/api/items/{item.id}/')
        assert r.data.get('is_favorited') is False

    def test_categories(self, guest, item):
        assert guest.get('/api/categories/').status_code == 200

    def test_auctions(self, guest):
        assert guest.get('/api/auctions/').status_code == 200

    def test_stores(self, guest):
        assert guest.get('/api/stores/').status_code == 200

    def test_reels(self, guest):
        assert guest.get('/api/reels/').status_code == 200


class TestGuestCannotAct:
    """The point of opening reads is that writes stay shut."""

    def test_cannot_create_an_item(self, guest):
        r = guest.post('/api/items/', {'title': 'x'}, format='json')
        assert r.status_code == 401

    def test_cannot_see_someone_elses_auctions(self, guest):
        assert guest.get('/api/auctions/my_auctions/').status_code == 401

    def test_cannot_reserve(self, guest, item):
        r = guest.post('/api/reservations/', {'item': item.id}, format='json')
        assert r.status_code == 401

    def test_cannot_read_chats(self, guest):
        assert guest.get('/api/chats/').status_code == 401

    def test_cannot_read_a_profile(self, guest):
        assert guest.get('/api/auth/profile/').status_code == 401


class TestTheGuestHomeScreen:
    """ما تناديه الشاشة الرئيسية فعلاً عند فتحها.

    الزائر كان لا يرى منتجات. السبب الأول كان في التطبيق — الشاشة تخرج
    قبل أن تطلب شيئاً إن كان الرمز `null` — والثاني هنا: نقطة «المقترَح»
    كانت IsAuthenticated فتردّ 401، فيبقى شريط المقترَح وشاشة «عرض الكل»
    فارغين لمن لا حساب له. وهذه الاختبارات تحرس الطرف الذي يملكه الخادم.
    """

    def test_recommended_opens_for_a_guest(self, guest):
        response = guest.get('/api/listings/fixed/recommended/')
        assert response.status_code == 200, response.data

    def test_a_guest_gets_the_popular_fallback_not_an_empty_page(
            self, guest, listing):
        # لا تفضيلات للزائر، فالترتيب يسقط إلى «الأكثر رواجاً» — وهو ما
        # تحسبه الدالّة أصلاً لكل من لا سجلّ له.
        body = guest.get('/api/listings/fixed/recommended/').data
        assert body['count'] >= 1
        assert any(r['item']['id'] == listing.item_id for r in body['results'])

    def test_it_is_paginated_like_the_main_listing(self, guest, listing):
        body = guest.get('/api/listings/fixed/recommended/?page_size=1').data
        assert {'count', 'next', 'previous', 'results'} <= set(body.keys())

    def test_two_guests_share_one_cached_ranking(self, guest, listing):
        # مفتاح واحد لكل الزوّار: لا `user.id` يُبنى عليه، ولا سبب لحساب
        # نفس القائمة لكل زائر.
        from django.core.cache import cache
        cache.clear()
        guest.get('/api/listings/fixed/recommended/')
        assert cache.get('recommended_listing_ids:anon') is not None

    def test_the_screens_a_guest_opens_all_answer(self, guest, listing):
        # ما تطلبه الشاشة الرئيسية عند الإقلاع، مجتمعاً.
        for path in ('/api/categories/', '/api/listings/fixed/',
                     '/api/listings/fixed/recommended/', '/api/items/',
                     '/api/auctions/', '/api/reels/'):
            assert guest.get(path).status_code == 200, path
