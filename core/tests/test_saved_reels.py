"""الريلز المحفوظة — ‎/api/reels/<id>/save/‎ و‎/unsave/‎ و‎/api/reels/saved/‎.

الحفظ جار للإعجاب في الشكل ومختلف عنه في المعنى: الإعجاب علنيّ (يرفع
`likes_count` ويُشعر صاحب المتجر عند العتبات)، والحفظ خاصّ لا يراه أحد غير
صاحبه. أكثر ما يُحرَس هنا هو هذا الفرق بالذات — فلو انزلق الحفظ إلى عدّاد
أو إشعار، لم يكن اختبار «نجح الحفظ» ليلاحظ.
"""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from core.models import Reel, SavedReel, User, UserStore

pytestmark = pytest.mark.django_db


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_store(username, name='متجر'):
    return UserStore.objects.create(user=make_user(username), name=name,
                                    is_active=True)


def make_reel(store, status='published', caption=''):
    return Reel.objects.create(
        store=store, caption=caption, status=status,
        video_key=f'reels/{store.id}/{caption or status}/video.mp4',
        thumbnail_key=f'reels/{store.id}/{caption or status}/thumb.jpg',
        duration_seconds=20,
    )


@pytest.fixture
def store():
    return make_store('tajir', 'متجر علي')


@pytest.fixture
def viewer():
    return make_user('mushahid')


@pytest.fixture
def client(viewer):
    c = APIClient()
    c.force_authenticate(user=viewer)
    return c


def save(client, reel_id):
    return client.post(f'/api/reels/{reel_id}/save/')


def unsave(client, reel_id):
    return client.post(f'/api/reels/{reel_id}/unsave/')


def saved(client):
    return client.get('/api/reels/saved/')


class TestSavingAndUnsaving:
    def test_saving_creates_one_row(self, client, store, viewer):
        reel = make_reel(store)
        response = save(client, reel.id)
        assert response.status_code == 200, response.data
        assert response.data['is_saved'] is True
        assert SavedReel.objects.filter(reel=reel, user=viewer).count() == 1

    def test_saving_twice_is_still_one_row(self, client, store, viewer):
        # ضغطة مزدوجة أو إعادة محاولة بعد شبكة بطيئة — لا صفّ ثانٍ ولا 500
        # من قيد التفرّد.
        reel = make_reel(store)
        save(client, reel.id)
        response = save(client, reel.id)
        assert response.status_code == 200
        assert SavedReel.objects.filter(reel=reel, user=viewer).count() == 1

    def test_unsaving_removes_it(self, client, store, viewer):
        reel = make_reel(store)
        save(client, reel.id)
        response = unsave(client, reel.id)
        assert response.status_code == 200
        assert response.data['is_saved'] is False
        assert not SavedReel.objects.filter(reel=reel, user=viewer).exists()

    def test_unsaving_what_was_never_saved_is_not_an_error(self, client, store):
        # الزرّ متفائل في التطبيق: قد يُرسل unsave لحالة الخادم فيها «غير
        # محفوظ» أصلاً، والجواب يجب أن يكون الحالة النهائية لا خطأً.
        reel = make_reel(store)
        response = unsave(client, reel.id)
        assert response.status_code == 200
        assert response.data['is_saved'] is False

    def test_two_viewers_save_the_same_reel_independently(self, store):
        reel = make_reel(store)
        for name in ('ahmad', 'kareem'):
            c = APIClient()
            c.force_authenticate(user=make_user(name))
            assert save(c, reel.id).status_code == 200
        assert SavedReel.objects.filter(reel=reel).count() == 2


class TestSavingIsPrivate:
    """الفرق الجوهري عن الإعجاب."""

    def test_no_counter_on_the_reel_moves(self, client, store):
        reel = make_reel(store)
        save(client, reel.id)
        reel.refresh_from_db()
        assert reel.likes_count == 0
        assert reel.view_count == 0

    def test_the_store_owner_is_not_notified(self, client, store):
        # لو وُصل الحفظ بـnotify_reel_liked لكان كل حفظ إشعاراً لصاحب
        # المتجر — وهو ما يجعل التاجر يُسكت إشعارات التطبيق كلّها.
        reel = make_reel(store)
        with patch('core.views.notify_reel_liked') as notify:
            save(client, reel.id)
        notify.assert_not_called()

    def test_a_save_is_not_a_like(self, client, store, viewer):
        reel = make_reel(store)
        save(client, reel.id)
        assert reel.likes.filter(user=viewer).exists() is False

    def test_one_viewers_save_is_invisible_to_another(self, client, store):
        reel = make_reel(store)
        save(client, reel.id)

        other = APIClient()
        other.force_authenticate(user=make_user('gharib'))
        card = other.get('/api/reels/', {'store': store.id}).data['results'][0]
        assert card['is_saved_by_me'] is False
        assert saved(other).data['results'] == []


class TestAnAccountIsRequired:
    """المحفوظات تخصّ شخصاً — فلا معنى لها بلا هوية."""

    @pytest.mark.parametrize('path', ['save', 'unsave'])
    def test_a_guest_cannot_save(self, store, path):
        reel = make_reel(store)
        r = APIClient().post(f'/api/reels/{reel.id}/{path}/')
        assert r.status_code in (401, 403)

    def test_a_guest_has_no_saved_list(self):
        assert APIClient().get('/api/reels/saved/').status_code in (401, 403)

    def test_a_guests_card_says_not_saved_rather_than_failing(self, store):
        # `get_is_saved_by_me` تقرأ request.user — والضيف بلا مستخدم.
        make_reel(store)
        card = APIClient().get('/api/reels/',
                               {'store': store.id}).data['results'][0]
        assert card['is_saved_by_me'] is False


class TestTheSavedList:
    def test_most_recently_saved_first(self, client, store):
        first = make_reel(store, caption='أول')
        second = make_reel(store, caption='ثانٍ')
        # نحفظ الأقدم نشراً أخيراً: الترتيب يتبع وقت **الحفظ** لا وقت النشر.
        save(client, first.id)
        save(client, second.id)
        ids = [r['id'] for r in saved(client).data['results']]
        assert ids == [second.id, first.id]

    def test_it_holds_reels_from_several_stores(self, client, store):
        other = make_store('tajir2', 'متجر ثانٍ')
        mine = make_reel(store)
        theirs = make_reel(other)
        save(client, mine.id)
        save(client, theirs.id)
        ids = {r['id'] for r in saved(client).data['results']}
        assert ids == {mine.id, theirs.id}

    def test_the_card_is_marked_saved(self, client, store):
        reel = make_reel(store)
        save(client, reel.id)
        assert saved(client).data['results'][0]['is_saved_by_me'] is True

    def test_an_unsaved_reel_leaves_the_list(self, client, store):
        reel = make_reel(store)
        save(client, reel.id)
        unsave(client, reel.id)
        assert saved(client).data['results'] == []

    @pytest.mark.parametrize('status', ['archived', 'deleted', 'failed'])
    def test_a_reel_that_stopped_being_published_drops_out(
            self, client, store, status):
        # إشارة محفوظة لريل أرشفه صاحبه أو حُذف: لا فيديو ليُشغَّل. اختفاؤه
        # من القائمة أرحم من مربّع يفتح على شاشة سوداء.
        reel = make_reel(store)
        save(client, reel.id)
        Reel.objects.filter(id=reel.id).update(status=status)
        assert saved(client).data['results'] == []

    def test_a_deleted_reel_takes_its_bookmark_with_it(self, client, store):
        reel = make_reel(store)
        save(client, reel.id)
        reel_id = reel.id
        reel.delete()
        # CASCADE: لا صفوف يتيمة تُعدّ في ترقيم الصفحات ثم تُسقَط عند الرسم،
        # فتظهر صفحة أقصر من حجمها بلا سبب ظاهر.
        assert not SavedReel.objects.filter(reel_id=reel_id).exists()
        assert saved(client).data['results'] == []

    def test_the_list_is_paginated(self, client, store):
        for i in range(3):
            save(client, make_reel(store, caption=f'ريل {i}').id)
        body = saved(client).data
        assert 'results' in body and 'count' in body
        assert body['count'] == 3
