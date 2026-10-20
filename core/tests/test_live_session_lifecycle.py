"""دورة حياة البث المباشر — ما وجدته مراجعة 2026-10-20.

ثلاثة أشياء تُحرَس هنا، كلّها وُجدت مكسورة:

١. **متجر واحد، بثّ واحد.** `create(start_immediately=True)` كان يفرض ذلك،
   و`go_live` لا يفرضه — فجلسةٌ تُجدَّل ثم تُبثّ تعطي المتجر بثّين.

٢. **المشاهد يُسجَّل خارجاً دائماً.** تتبّع المشاهد كان معلّقاً بنجاح إنقاص
   العدّاد، و`end_live_session` يُصفّر العدّاد — فكل من كان متّصلاً لحظة
   انتهاء البث يبقى `is_watching=True` إلى الأبد.

٣. **عدّاد المشاهدين لا ينزل تحت الصفر** — السلوك القديم الذي يجب ألّا
   يتغيّر بإصلاح (٢).
"""
import pytest
from asgiref.sync import async_to_sync
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from core.live_utils import end_live_session
from core.models import LiveSession, LiveSessionViewer, User, UserStore

pytestmark = pytest.mark.django_db


def make_store(username='seller'):
    user = User.objects.create_user(username=username, password='Passw0rd!x')
    return UserStore.objects.create(user=user, name='متجر', is_active=True), user


def make_session(store, status='scheduled'):
    return LiveSession.objects.create(
        store=store, title='بث', status=status,
        started_at=timezone.now() if status == 'live' else None)


@pytest.fixture
def owner_client():
    store, user = make_store()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, store, user


class TestOneLiveSessionPerStore:
    def test_go_live_is_refused_while_another_is_live(self, owner_client):
        client, store, _ = owner_client
        make_session(store, status='live')
        scheduled = make_session(store, status='scheduled')

        response = client.post(reverse('live-session-go-live',
                                       args=[scheduled.id]))
        assert response.status_code == 400, response.data
        scheduled.refresh_from_db()
        assert scheduled.status == 'scheduled'
        assert LiveSession.objects.filter(store=store, status='live').count() == 1

    def test_go_live_works_once_the_other_one_ended(self, owner_client):
        client, store, _ = owner_client
        first = make_session(store, status='live')
        scheduled = make_session(store, status='scheduled')

        end_live_session(first)
        response = client.post(reverse('live-session-go-live',
                                       args=[scheduled.id]))
        assert response.status_code == 200, response.data
        scheduled.refresh_from_db()
        assert scheduled.status == 'live'
        assert scheduled.started_at is not None

    def test_another_stores_live_session_is_no_obstacle(self, owner_client):
        # القيد لكل متجر لا للمنصّة: بثّ متجر آخر لا يمنع هذا.
        client, store, _ = owner_client
        other_store, _ = make_store('seller2')
        make_session(other_store, status='live')
        scheduled = make_session(store, status='scheduled')

        assert client.post(reverse('live-session-go-live',
                                   args=[scheduled.id])).status_code == 200

    def test_creating_a_second_live_session_is_still_refused(self, owner_client):
        # السلوك الذي كان سليماً أصلاً — لا يتغيّر.
        client, store, _ = owner_client
        make_session(store, status='live')
        response = client.post(reverse('live-session-list'),
                               {'title': 'بث ثانٍ', 'start_immediately': True},
                               format='json')
        assert response.status_code == 400


# transaction=True وليس المعاملة المعتادة: دوالّ المشاهد مزيَّنة بـ
# `database_sync_to_async`، فتُنفَّذ على خيط آخر باتصال آخر لا يرى معاملة
# الاختبار — والنتيجة `MySQL server has gone away` لا فشل منطقي. الالتزام
# الحقيقي يجعل الخيط الثاني يرى الصفوف، ويُنفّذ المسار كما يُنفَّذ في
# الإنتاج بما فيه الانتقال بين الخيوط.
@pytest.mark.django_db(transaction=True)
class TestTheViewerIsAlwaysSignedOut:
    """تتبّع المشاهد لا يتعلّق بالعدّاد — همّان مستقلّان."""

    def make_consumer(self, session, user):
        """مستهلك حقيقي بطبقة قنوات صامتة.

        الطبقة تُستبدَل لا تُستغنى عنها: `disconnect` ينادي group_discard
        و group_send، والمطلوب اختبار **ترتيب** ما يفعله حولهما لا البثّ
        نفسه. وهذا مقصود — العطب كان في `disconnect` لا في
        `track_viewer_left`، فاختبارٌ ينادي الدالّتين بالترتيب الصحيح بنفسه
        كان سيمرّ على الكود المعطوب أيضاً.
        """
        from core.consumers import LiveSessionConsumer

        class _SilentLayer:
            async def group_discard(self, *a, **kw):
                return None

            async def group_send(self, *a, **kw):
                return None

        consumer = LiveSessionConsumer()
        consumer.session_id = session.id
        consumer.user = user
        consumer.room_group_name = 'live_%s' % session.id
        consumer.channel_name = 'test-channel'
        consumer.channel_layer = _SilentLayer()
        return consumer

    def connect_and_disconnect(self, session, user, *, zero_the_counter=False):
        consumer = self.make_consumer(session, user)

        async_to_sync(consumer.track_viewer_joined)()
        async_to_sync(consumer.increment_viewer_count)()

        if zero_the_counter:
            # هذا ما يفعله end_live_session بالضبط.
            LiveSession.objects.filter(id=session.id).update(
                current_viewer_count=0)

        # الدالّة الحقيقية، لا محاكاة لخطواتها.
        async_to_sync(consumer.disconnect)(1000)

    def test_a_viewer_who_leaves_normally_stops_watching(self):
        store, _ = make_store()
        session = make_session(store, status='live')
        viewer = User.objects.create_user(username='v1', password='x')

        self.connect_and_disconnect(session, viewer)

        row = LiveSessionViewer.objects.get(session=session, user=viewer)
        assert row.active_connections == 0
        assert row.is_watching is False

    def test_a_viewer_still_connected_when_the_stream_ends_stops_watching(self):
        # العطب: end_live_session يُصفّر العدّاد، فكان إنقاصه يفشل، فلا
        # يُنادى track_viewer_left أبداً — ويبقى الصفّ «يشاهد» إلى الأبد.
        store, _ = make_store()
        session = make_session(store, status='live')
        viewer = User.objects.create_user(username='v2', password='x')

        self.connect_and_disconnect(session, viewer, zero_the_counter=True)

        row = LiveSessionViewer.objects.get(session=session, user=viewer)
        assert row.active_connections == 0
        assert row.is_watching is False

    def test_two_devices_keep_watching_until_both_close(self):
        store, _ = make_store()
        session = make_session(store, status='live')
        viewer = User.objects.create_user(username='v3', password='x')

        consumer = self.make_consumer(session, viewer)

        async_to_sync(consumer.track_viewer_joined)()
        async_to_sync(consumer.track_viewer_joined)()
        async_to_sync(consumer.track_viewer_left)()

        row = LiveSessionViewer.objects.get(session=session, user=viewer)
        assert row.active_connections == 1
        assert row.is_watching is True, 'جهاز واحد أُغلق، والثاني ما زال يشاهد'

        async_to_sync(consumer.track_viewer_left)()
        row.refresh_from_db()
        assert row.is_watching is False

    def test_the_counter_never_goes_negative(self):
        store, _ = make_store()
        session = make_session(store, status='live')
        consumer = self.make_consumer(
            session, User.objects.create_user(username='v4', password='x'))

        # إنقاصان بلا زيادة — الحارس `current_viewer_count__gt=0` باقٍ.
        async_to_sync(consumer.decrement_viewer_count)()
        async_to_sync(consumer.decrement_viewer_count)()

        session.refresh_from_db()
        assert session.current_viewer_count == 0


class TestEndingASession:
    def test_ending_resets_the_counter_and_skips_the_queue(self, owner_client):
        client, store, _ = owner_client
        session = make_session(store, status='live')
        LiveSession.objects.filter(id=session.id).update(current_viewer_count=7)

        response = client.post(reverse('live-session-end-session',
                                       args=[session.id]))
        assert response.status_code == 200, response.data
        session.refresh_from_db()
        assert session.status == 'ended'
        assert session.current_viewer_count == 0
        assert session.ended_at is not None

    def test_a_session_cannot_be_ended_twice(self, owner_client):
        client, store, _ = owner_client
        session = make_session(store, status='live')
        client.post(reverse('live-session-end-session', args=[session.id]))
        again = client.post(reverse('live-session-end-session',
                                    args=[session.id]))
        assert again.status_code == 400

    def test_another_store_cannot_end_this_session(self, owner_client):
        # IsStoreOwner.has_object_permission — يُحرَس هنا لأن go_live و
        # end_session كلاهما يعتمد عليه وحده في منع التدخّل.
        _, store, _ = owner_client
        session = make_session(store, status='live')

        _, intruder = make_store('seller3')
        other = APIClient()
        other.force_authenticate(user=intruder)

        response = other.post(reverse('live-session-end-session',
                                      args=[session.id]))
        assert response.status_code in (403, 404)
        session.refresh_from_db()
        assert session.status == 'live'
