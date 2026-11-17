"""الردود على تعليقات الريلز.

مستوى واحد كإنستغرام: الردّ على ردّ يُعلَّق تحت التعليق الرئيسي ويحمل اسم
من رُدّ عليه. وأهمّ ما يُختبر بعد الشكل: أن يصل الإشعار إلى من رُدّ عليه،
مرّة واحدة لا اثنتين، وألّا يُشعَر أحد بردّه على نفسه.
"""
import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Notification, Reel, ReelComment, UserStore

User = get_user_model()
pytestmark = pytest.mark.django_db


def make_user(username, phone):
    return User.objects.create_user(username=username, password='x', phone=phone)


@pytest.fixture
def seller():
    return make_user('seller', '07701234567')


@pytest.fixture
def buyer():
    return make_user('buyer', '07801234567')


@pytest.fixture
def other():
    return make_user('other', '07901234567')


@pytest.fixture
def reel(seller):
    store = UserStore.objects.create(user=seller, name='متجر علي', is_active=True)
    return Reel.objects.create(
        store=store, status='published',
        video_key=f'reels/{store.id}/abc/video.mp4',
        thumbnail_key=f'reels/{store.id}/abc/thumb.jpg',
        duration_seconds=15, published_at=timezone.now(),
    )


def as_user(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def post(user, reel, content, parent_id=None):
    body = {'content': content}
    if parent_id is not None:
        body['parent_id'] = parent_id
    return as_user(user).post(f'/api/reels/{reel.id}/comments/', body, format='json')


def notes(user, kind):
    return Notification.objects.filter(user=user, notification_type=kind)


class TestThreading:
    def test_a_reply_sits_under_its_comment(self, reel, seller, buyer):
        question = post(buyer, reel, 'بكم هذا؟').data['data']
        res = post(seller, reel, '25 ألف', parent_id=question['id'])
        assert res.status_code == 201
        assert res.data['data']['parent_id'] == question['id']

        listing = APIClient().get(f'/api/reels/{reel.id}/comments/').data['data']
        assert [c['id'] for c in listing] == [question['id']]
        assert [r['content'] for r in listing[0]['replies']] == ['25 ألف']

    def test_replying_to_a_reply_stays_one_level_deep(self, reel, seller, buyer, other):
        question = post(buyer, reel, 'بكم هذا؟').data['data']
        answer = post(seller, reel, '25 ألف', parent_id=question['id']).data['data']
        res = post(other, reel, 'وبالجملة؟', parent_id=answer['id'])

        assert res.data['data']['parent_id'] == question['id']
        assert res.data['data']['reply_to_username'] == 'seller'

    def test_replies_come_oldest_first(self, reel, seller, buyer, other):
        question = post(buyer, reel, 'متوفر؟').data['data']
        post(seller, reel, 'نعم', parent_id=question['id'])
        post(other, reel, 'وأنا أريد', parent_id=question['id'])
        replies = APIClient().get(
            f'/api/reels/{reel.id}/comments/').data['data'][0]['replies']
        assert [r['content'] for r in replies] == ['نعم', 'وأنا أريد']

    def test_the_seller_is_marked(self, reel, seller, buyer):
        question = post(buyer, reel, 'متوفر؟').data['data']
        post(seller, reel, 'نعم', parent_id=question['id'])
        thread = APIClient().get(f'/api/reels/{reel.id}/comments/').data['data'][0]
        assert thread['is_store_owner'] is False
        assert thread['replies'][0]['is_store_owner'] is True

    def test_replying_to_a_comment_on_another_reel_is_refused(self, reel, seller, buyer):
        other_reel = Reel.objects.create(
            store=reel.store, status='published', video_key='v', thumbnail_key='t',
            duration_seconds=5, published_at=timezone.now(),
        )
        foreign = post(buyer, other_reel, 'سؤال').data['data']
        res = post(seller, reel, 'جواب', parent_id=foreign['id'])
        assert res.status_code == 404
        assert not ReelComment.objects.filter(content='جواب').exists()


class TestCounter:
    def test_replies_count_toward_the_reel(self, reel, seller, buyer):
        question = post(buyer, reel, 'بكم؟').data['data']
        post(seller, reel, '10', parent_id=question['id'])
        reel.refresh_from_db()
        assert reel.comments_count == 2

    def test_deleting_a_comment_takes_its_replies_off_the_counter(
            self, reel, seller, buyer, other):
        question = post(buyer, reel, 'بكم؟').data['data']
        post(seller, reel, '10', parent_id=question['id'])
        post(other, reel, 'شكراً', parent_id=question['id'])
        res = as_user(seller).delete(
            f'/api/reels/{reel.id}/comments/{question["id"]}/')
        assert res.data['removed'] == 3
        reel.refresh_from_db()
        assert reel.comments_count == 0
        assert ReelComment.objects.count() == 0


class TestReplyNotifications:
    def test_the_commenter_hears_the_sellers_answer(self, reel, seller, buyer):
        question = post(buyer, reel, 'بكم؟').data['data']
        post(seller, reel, '25 ألف', parent_id=question['id'])
        note = notes(buyer, 'reel_comment_reply').get()
        assert '25 ألف' in note.body
        assert note.data['reel_id'] == str(reel.id)

    def test_a_reply_to_a_reply_notifies_who_was_answered(
            self, reel, seller, buyer, other):
        question = post(buyer, reel, 'بكم؟').data['data']
        answer = post(seller, reel, '25', parent_id=question['id']).data['data']
        post(other, reel, 'وبالجملة؟', parent_id=answer['id'])
        assert notes(seller, 'reel_comment_reply').count() == 1
        # إشعار المشتري الوحيد هو جواب البائع؛ ردّ الثالث لم يكن له.
        assert notes(buyer, 'reel_comment_reply').count() == 1

    def test_the_seller_gets_one_note_not_two(self, reel, seller, buyer):
        # ردّ زبون على تعليق البائع: «ردّ على تعليقك» يكفي، بلا «تعليق جديد».
        mine = post(seller, reel, 'خصم اليوم').data['data']
        post(buyer, reel, 'إلى متى؟', parent_id=mine['id'])
        assert notes(seller, 'reel_comment_reply').count() == 1
        assert notes(seller, 'reel_comment').count() == 0

    def test_the_seller_still_hears_buyers_talking_on_the_reel(
            self, reel, seller, buyer, other):
        question = post(buyer, reel, 'جيد؟').data['data']
        post(other, reel, 'اشتريته وممتاز', parent_id=question['id'])
        assert notes(seller, 'reel_comment').count() == 2
        assert notes(buyer, 'reel_comment_reply').count() == 1

    def test_replying_to_yourself_notifies_nobody_new(self, reel, seller, buyer):
        question = post(buyer, reel, 'بكم؟').data['data']
        post(buyer, reel, 'والتوصيل؟', parent_id=question['id'])
        assert notes(buyer, 'reel_comment_reply').count() == 0
