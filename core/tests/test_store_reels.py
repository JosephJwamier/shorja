"""ريلزات متجر واحد: ‎GET /api/reels/?store=<id>‎.

هذا هو العقد الذي يقف عليه تبويب «الريلز» في شاشة المتجر داخل التطبيق.
النقطة نفسها قديمة (خلاصة الريلز العامة تستعملها بلا مُرشِّح)، والمُرشِّح
`store` و`AllowAny` كانا موجودين قبل هذه الميزة — لكن لم يستعملهما أحد، وما
لا يستعمله أحد لا يحرسه شيء. فما يُثبَّت هنا هو ما يراه **الزائر**: متجر
واحد، منشور فقط، بلا حساب.
"""
import pytest
from rest_framework.test import APIClient

from core.models import Category, Item, Reel, User, UserStore

pytestmark = pytest.mark.django_db


def make_store(username, name):
    user = User.objects.create_user(username=username, password='pass12345')
    return UserStore.objects.create(user=user, name=name, is_active=True)


def make_reel(store, status='published', caption='', item=None):
    return Reel.objects.create(
        store=store, item=item, caption=caption, status=status,
        video_key=f'reels/{store.id}/{caption or status}/video.mp4',
        thumbnail_key=f'reels/{store.id}/{caption or status}/thumb.jpg',
        duration_seconds=20,
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def store():
    return make_store('tajir', 'متجر علي')


def fetch(client, store_id):
    return client.get('/api/reels/', {'store': store_id})


class TestAVisitorWithoutAnAccount:
    """التبويب يُفتح للضيف — لا يحتاج حساباً ليشاهد ريلزات متجر."""

    def test_the_list_opens_with_no_token(self, client, store):
        make_reel(store, caption='أول ريل')
        response = fetch(client, store.id)
        assert response.status_code == 200, response.data
        assert len(response.data['results']) == 1

    def test_is_liked_by_me_is_false_not_an_error(self, client, store):
        # الضيف بلا مستخدم، و`get_is_liked_by_me` تقرأ request.user.
        make_reel(store)
        results = fetch(client, store.id).data['results']
        assert results[0]['is_liked_by_me'] is False


class TestOnlyThisStoresPublishedReels:
    def test_another_stores_reels_do_not_appear(self, client, store):
        other = make_store('tajir2', 'متجر ثانٍ')
        mine = make_reel(store, caption='لي')
        make_reel(other, caption='له')
        results = fetch(client, store.id).data['results']
        assert [r['id'] for r in results] == [mine.id]

    @pytest.mark.parametrize(
        'status', ['pending_upload', 'failed', 'archived', 'deleted'])
    def test_an_unpublished_reel_is_hidden(self, client, store, status):
        # الزائر يفتح ملف المتجر؛ ريل يُرفع الآن أو فشل رفعه أو حُذف ليس
        # محتوىً يُعرض عليه — وصفّ pending_upload بلا فيديو في التخزين
        # يعني مشغّلاً يدور على لا شيء.
        make_reel(store, status=status)
        assert fetch(client, store.id).data['results'] == []

    def test_newest_first(self, client, store):
        first = make_reel(store, caption='الأقدم')
        second = make_reel(store, caption='الأحدث')
        results = fetch(client, store.id).data['results']
        assert [r['id'] for r in results] == [second.id, first.id]

    def test_an_empty_store_is_an_empty_list_not_a_404(self, client, store):
        # شبكة فارغة تعرض «لا توجد ريلزات»؛ 404 كانت ستعرض خطأً.
        response = fetch(client, store.id)
        assert response.status_code == 200
        assert response.data['results'] == []


class TestWhatTheGridNeedsToDraw:
    """كل حقل هنا يرسمه التطبيق فعلاً — وغيابه يترك فراغاً في الواجهة."""

    def test_the_card_carries_a_thumbnail_and_a_video_url(self, client, store):
        make_reel(store)
        card = fetch(client, store.id).data['results'][0]
        assert card['thumbnail_url'].startswith('http')
        assert card['video_url'].startswith('http')

    def test_the_card_carries_the_caption(self, client, store):
        # كان `caption` في ReelDetailSerializer وحده، فخلاصة الريلز العامة
        # وكل شبكة متجر كانت تعرض وصفاً فارغاً لكل ريل.
        make_reel(store, caption='بضاعة وصلت اليوم')
        card = fetch(client, store.id).data['results'][0]
        assert card['caption'] == 'بضاعة وصلت اليوم'

    def test_the_card_carries_the_counters_the_tiles_show(self, client, store):
        reel = make_reel(store)
        Reel.objects.filter(id=reel.id).update(
            view_count=140, likes_count=12, comments_count=3)
        card = fetch(client, store.id).data['results'][0]
        assert (card['view_count'], card['likes_count'],
                card['comments_count']) == (140, 12, 3)

    def test_a_tagged_product_survives_into_the_card(self, client, store):
        category = Category.objects.create(name='هواتف', slug='phones')
        item = Item.objects.create(
            owner=store.user, title='Galaxy', description='d',
            category=category, condition='new', quantity=2, status='active')
        make_reel(store, item=item)
        card = fetch(client, store.id).data['results'][0]
        assert card['item'] is not None
        assert card['item']['id'] == item.id
