"""إشعارات الإعجاب والتعليق على الريلز.

أهمّ ما يُختبر هنا ليس أن الإشعار يصل، بل أنه **لا يصل في كل مرة**: ريل
ناجح يجمع مئات الإعجابات، وإشعار لكل واحد يدفع التاجر إلى إطفاء إشعارات
التطبيق كلها — فيخسر معها إشعارات الحجوزات والمزادات، وهي التي تبيع.
"""
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Notification, Reel, UserStore

User = get_user_model()
pytestmark = pytest.mark.django_db


def make_user(username, phone):
    return User.objects.create_user(username=username, password='x', phone=phone)


@pytest.fixture
def owner():
    return make_user('owner', '07701234567')


@pytest.fixture
def reel(owner):
    store = UserStore.objects.create(user=owner, name='متجر علي', is_active=True)
    return Reel.objects.create(
        store=store, status='published',
        video_key=f'reels/{store.id}/abc/video.mp4',
        thumbnail_key=f'reels/{store.id}/abc/thumb.jpg',
        duration_seconds=15, published_at=timezone.now(),
    )


@pytest.fixture
def viewer():
    return make_user('viewer', '07801234567')


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def reel_notifications(user, kind):
    # ترتيب صريح: Notification.Meta يرتّب تنازلياً بالتاريخ، فـ .first()
    # و.last() تعنيان عكس ما يُتوقّع من ترتيب الإنشاء.
    return Notification.objects.filter(
        user=user, notification_type=kind).order_by('id')


class TestLikeThrottling:
    def test_the_first_like_notifies(self, reel, owner, viewer):
        as_user(viewer).post(f'/api/reels/{reel.id}/like/')
        assert reel_notifications(owner, 'reel_liked').count() == 1

    def test_likes_between_milestones_stay_silent(self, reel, owner):
        # تسعة معجبين: الأول يُشعر، والثمانية بعده لا.
        for i in range(9):
            user = make_user(f'fan{i}', f'0770000{i:04d}')
            as_user(user).post(f'/api/reels/{reel.id}/like/')

        reel.refresh_from_db()
        assert reel.likes_count == 9
        assert reel_notifications(owner, 'reel_liked').count() == 1

    def test_the_tenth_like_is_a_milestone(self, reel, owner):
        for i in range(10):
            user = make_user(f'fan{i}', f'0770000{i:04d}')
            as_user(user).post(f'/api/reels/{reel.id}/like/')

        # الأول والعاشر فقط.
        assert reel_notifications(owner, 'reel_liked').count() == 2
        assert '10' in reel_notifications(owner, 'reel_liked').last().body

    def test_liking_your_own_reel_notifies_nobody(self, reel, owner):
        as_user(owner).post(f'/api/reels/{reel.id}/like/')
        assert reel_notifications(owner, 'reel_liked').count() == 0

    def test_liking_twice_does_not_notify_twice(self, reel, owner, viewer):
        client = as_user(viewer)
        client.post(f'/api/reels/{reel.id}/like/')
        client.post(f'/api/reels/{reel.id}/like/')
        # get_or_create لا ينشئ الثاني، فلا عدّاد يتقدّم ولا إشعار يُرسل.
        assert reel_notifications(owner, 'reel_liked').count() == 1

    def test_unlike_then_like_does_not_re_announce_the_milestone(
            self, reel, owner, viewer):
        client = as_user(viewer)
        client.post(f'/api/reels/{reel.id}/like/')
        client.post(f'/api/reels/{reel.id}/unlike/')
        client.post(f'/api/reels/{reel.id}/like/')
        # العدّاد يعود إلى 1 فيقع على العتبة ثانية — سلوك مقبول لأن
        # الوصول إليه يتطلب إلغاءً متعمّداً، وليس مساراً يُطرَق صدفة.
        assert reel_notifications(owner, 'reel_liked').count() == 2


class TestCommentNotifications:
    def test_every_comment_notifies(self, reel, owner, viewer):
        client = as_user(viewer)
        for text in ['بكم؟', 'هل متوفر بالأزرق؟', 'أريد قطعتين']:
            client.post(f'/api/reels/{reel.id}/comments/',
                        {'content': text}, format='json')
        # التعليق سؤال عن البضاعة، وتأخيره يُضيّع بيعاً — فلا عتبات.
        assert reel_notifications(owner, 'reel_comment').count() == 3

    def test_the_comment_text_reaches_the_notification(self, reel, owner, viewer):
        as_user(viewer).post(f'/api/reels/{reel.id}/comments/',
                             {'content': 'بكم هذا؟'}, format='json')
        assert 'بكم هذا؟' in reel_notifications(owner, 'reel_comment').first().body

    def test_a_long_comment_is_cut_with_an_ellipsis(self, reel, owner, viewer):
        as_user(viewer).post(f'/api/reels/{reel.id}/comments/',
                             {'content': 'ممتاز ' * 40}, format='json')
        body = reel_notifications(owner, 'reel_comment').first().body
        # يُعرض في سطر على شاشة القفل؛ القطع بإشارة أوضح من قطع صامت.
        assert body.endswith('…')
        assert len(body) < 120

    def test_commenting_on_your_own_reel_notifies_nobody(self, reel, owner):
        as_user(owner).post(f'/api/reels/{reel.id}/comments/',
                            {'content': 'شكراً للمتابعة'}, format='json')
        assert reel_notifications(owner, 'reel_comment').count() == 0


class TestNotificationPayload:
    def test_a_like_carries_the_reel_id_so_the_tap_opens_it(
            self, reel, owner, viewer):
        as_user(viewer).post(f'/api/reels/{reel.id}/like/')
        data = reel_notifications(owner, 'reel_liked').first().data
        assert data['reel_id'] == str(reel.id)

    def test_a_comment_carries_both_ids(self, reel, owner, viewer):
        as_user(viewer).post(f'/api/reels/{reel.id}/comments/',
                             {'content': 'بكم؟'}, format='json')
        data = reel_notifications(owner, 'reel_comment').first().data
        assert data['reel_id'] == str(reel.id)
        assert 'comment_id' in data
