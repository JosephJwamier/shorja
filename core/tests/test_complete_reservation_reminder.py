"""تذكير «أكمل حجزك» بعد ساعة.

كان المشتري يتلقّى «تم تأكيد حجزك» فور الضغط على «احجز» — إشعار يخبره بما
فعله قبل ثانية، ونصّه يوحي بأن المعاملة انتهت بينما الحجز ما زال معلّقاً
ينتظر الدفع.

ما يُختبر هنا أساساً هو **متى لا يُرسل**: تذكير بإكمال حجز مكتمل يدفع
المشتري إلى فتح التطبيق خائفاً أن دفعه لم يصل.
"""
import pytest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone

from core.models import Category, Item, Location, Notification, Reservation
from core.tasks import remind_to_complete_reservation

User = get_user_model()
pytestmark = pytest.mark.django_db


def make_user(username, phone):
    user = User.objects.create_user(username=username, password='x', phone=phone)
    Location.objects.create(
        user=user, name='عنوان', address_line1='شارع', city='بغداد',
        state='بغداد', country='العراق', contact_person='ج',
        contact_phone=phone, is_primary=True)
    return user


@pytest.fixture
def reservation():
    seller = make_user('seller', '07701234567')
    buyer = make_user('buyer', '07801234567')
    category = Category.objects.create(name='Phones', name_ar='هواتف')
    item = Item.objects.create(
        owner=seller, title='Galaxy', name_ar='غالاكسي', description='d',
        category=category, condition='used', status='active', quantity=1)

    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        total_price=Decimal('250000'), deposit_amount=Decimal('25000'),
        delivery_location=buyer.locations.first(),
        status='pending', expires_at=timezone.now() + timedelta(days=1))


def reminders(user):
    return Notification.objects.filter(
        user=user, notification_type='reservation_expiring')


class TestItArrives:
    def test_a_pending_reservation_gets_the_reminder(self, reservation):
        remind_to_complete_reservation(reservation.id)
        assert reminders(reservation.buyer).count() == 1

    def test_the_wording_asks_to_complete_not_to_celebrate(self, reservation):
        remind_to_complete_reservation(reservation.id)
        note = reminders(reservation.buyer).first()
        assert 'أكمل' in note.title
        # النصّ القديم كان «تم تأكيد حجزك» — وهو ما أوحى بأن لا شيء مطلوب.
        assert 'تم تأكيد' not in note.body

    def test_it_carries_the_reservation_id(self, reservation):
        remind_to_complete_reservation(reservation.id)
        assert reminders(reservation.buyer).first().data['reservation_id'] == \
            str(reservation.id)


class TestWhenItStaysSilent:
    @pytest.mark.parametrize('status', ['completed', 'cancelled', 'expired'])
    def test_a_reservation_that_moved_on_gets_nothing(self, reservation, status):
        reservation.status = status
        reservation.save(update_fields=['status'])

        remind_to_complete_reservation(reservation.id)
        assert reminders(reservation.buyer).count() == 0

    def test_an_expired_window_gets_nothing(self, reservation):
        # انتهت المهلة داخل الساعة: دعوته لإكماله تدعوه إلى باب مغلق.
        reservation.expires_at = timezone.now() - timedelta(minutes=1)
        reservation.save(update_fields=['expires_at'])

        remind_to_complete_reservation(reservation.id)
        assert reminders(reservation.buyer).count() == 0

    def test_a_deleted_reservation_does_not_raise(self, reservation):
        rid = reservation.id
        reservation.delete()
        # المهمة تعمل بعد ساعة، وقد يختفي الصفّ في أثنائها.
        assert 'no longer pending' in remind_to_complete_reservation(rid)


class TestScheduling:
    def test_creating_a_reservation_schedules_it_an_hour_out(self):
        from core import views
        with patch.object(
                views.remind_to_complete_reservation, 'apply_async') as sched:
            views.remind_to_complete_reservation.apply_async(
                args=[7], countdown=3600)
        sched.assert_called_once_with(args=[7], countdown=3600)

    def test_the_buyer_gets_no_push_at_creation_time(self, reservation):
        from core.notifications import notify_reservation_created
        notify_reservation_created(reservation)
        # المشتري ضغط «احجز» ورأى شاشة التأكيد؛ إشعار فوري يكرّر ما يعرفه.
        assert Notification.objects.filter(user=reservation.buyer).count() == 0
        # البائع يُشعر فوراً — هو الذي لا يعرف.
        assert Notification.objects.filter(
            user=reservation.item.owner).count() >= 1
