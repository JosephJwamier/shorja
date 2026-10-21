"""تذكير البائع بتسليم الطلب لشركة التوصيل.

رسالة واتساب تُرسَل إلى تاجر حقيقي، فأكثر ما يُحرَس هنا هو ما **لا**
يُرسَل: طلبٌ سُلّم، أو أُلغي، أو لم يُدفع، أو ذُكّر صاحبه أمس، أو كانت
الساعة الثالثة فجراً. تذكيرٌ خاطئ واحد يكفي ليُسكت التاجر الرقم — فيخسر
معه إشعارات الحجز والمزاد، وهي التي تُدرّ عليه مالاً.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from core.models import (
    Category, DeliveryOrder, Item, Reservation, User, UserStore,
)
from core.reminder_window import (
    is_within_window, next_window_opening, seconds_until_window,
)
from core.tasks import remind_sellers_to_hand_over
from core.tests.phones import next_phone

BAGHDAD = ZoneInfo('Asia/Baghdad')


# ---------------------------------------------------------------------------
# نافذة الدوام — منطق خالص
# ---------------------------------------------------------------------------

def at(hour, minute=0):
    return datetime(2026, 10, 21, hour, minute, tzinfo=BAGHDAD)


class TestTheWindow:
    @pytest.mark.parametrize('hour, inside', [
        (3, False), (8, False), (9, True), (14, True),
        (20, True), (21, False), (23, False),
    ])
    def test_boundaries(self, hour, inside):
        # الفتح شامل والإغلاق غير شامل: 21 تعني «حتى التاسعة مساءً».
        assert is_within_window(at(hour), start_hour=9, end_hour=21) is inside

    def test_inside_the_window_means_now(self):
        moment = at(14, 30)
        assert next_window_opening(moment, start_hour=9, end_hour=21) == moment
        assert seconds_until_window(moment, start_hour=9, end_hour=21) == 0

    def test_before_opening_waits_for_today(self):
        opening = next_window_opening(at(6), start_hour=9, end_hour=21)
        assert (opening.day, opening.hour) == (21, 9)

    def test_after_closing_waits_for_tomorrow(self):
        # الثالثة فجراً هي الحالة التي وُجدت النافذة لأجلها.
        opening = next_window_opening(at(23), start_hour=9, end_hour=21)
        assert (opening.day, opening.hour) == (22, 9)

    def test_seconds_are_never_negative(self):
        for hour in range(24):
            assert seconds_until_window(at(hour), start_hour=9, end_hour=21) >= 0


# ---------------------------------------------------------------------------
# المهمّة
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.django_db


@pytest.fixture
def paid_reservation():
    """حجز دفعه المشتري قبل يومين ولم يُسلَّم."""
    seller = User.objects.create_user(
        username='tajir', password='x', phone=next_phone(), first_name='علي')
    UserStore.objects.create(user=seller, name='متجر علي', is_active=True)
    buyer = User.objects.create_user(
        username='mushtari', password='x', phone=next_phone())
    category = Category.objects.create(name='Phones', name_ar='هواتف')
    item = Item.objects.create(
        owner=seller, title='Galaxy', description='d', category=category,
        condition='new', quantity=1, status='reserved')
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1,
        total_price=Decimal('250000'), deposit_amount=Decimal('25000'),
        status='completed',
        completed_at=timezone.now() - timedelta(hours=48),
        expires_at=timezone.now() + timedelta(days=7),
    )


def run_inside_window():
    """يُشغّل المهمّة كأن الساعة الثانية ظهراً ببغداد."""
    noon = timezone.localtime().replace(hour=14, minute=0, second=0)
    with patch('core.tasks.timezone.localtime', return_value=noon):
        return remind_sellers_to_hand_over()


class TestWhenItSends:
    def test_a_paid_undelivered_order_is_reminded(self, paid_reservation):
        with patch('core.notifications.notify_push') as push:
            result = run_inside_window()
        assert 'Sent 1' in result, result
        push.assert_called_once()
        paid_reservation.refresh_from_db()
        assert paid_reservation.handover_reminder_sent_at is not None

    def test_the_seller_is_the_recipient(self, paid_reservation):
        with patch('core.notifications.notify_push') as push:
            run_inside_window()
        assert push.call_args.args[0] == paid_reservation.item.owner


class TestWhenItStaysSilent:
    def test_already_handed_to_the_delivery_company(self, paid_reservation):
        DeliveryOrder.objects.create(
            reservation=paid_reservation, direction='outbound',
            from_name='علي', from_phone='07701234567', from_address='بغداد',
            to_name='مشتري', to_phone='07701234568', to_address='بغداد')
        with patch('core.notifications.notify_push') as push:
            assert 'Sent 0' in run_inside_window()
        push.assert_not_called()

    def test_not_yet_paid(self, paid_reservation):
        # 'pending' يعني أن المشتري لم يدفع — فلا واجب على البائع بعد.
        paid_reservation.status = 'pending'
        paid_reservation.save(update_fields=['status'])
        with patch('core.notifications.notify_push') as push:
            assert 'Sent 0' in run_inside_window()
        push.assert_not_called()

    @pytest.mark.parametrize('status', ['cancelled', 'refunded', 'disputed'])
    def test_a_dead_reservation(self, paid_reservation, status):
        paid_reservation.status = status
        paid_reservation.save(update_fields=['status'])
        with patch('core.notifications.notify_push') as push:
            assert 'Sent 0' in run_inside_window()
        push.assert_not_called()

    def test_paid_less_than_the_delay_ago(self, paid_reservation):
        paid_reservation.completed_at = timezone.now() - timedelta(hours=3)
        paid_reservation.save(update_fields=['completed_at'])
        with patch('core.notifications.notify_push') as push:
            assert 'Sent 0' in run_inside_window()
        push.assert_not_called()

    def test_never_twice(self, paid_reservation):
        with patch('core.notifications.notify_push'):
            run_inside_window()
        with patch('core.notifications.notify_push') as push:
            assert 'Sent 0' in run_inside_window()
        push.assert_not_called(), 'رسالة كل بضع ساعات تُنتج رقماً محظوراً'

    def test_nothing_goes_out_at_three_in_the_morning(self, paid_reservation):
        dawn = timezone.localtime().replace(hour=3, minute=0, second=0)
        with patch('core.tasks.timezone.localtime', return_value=dawn), \
                patch('core.notifications.notify_push') as push:
            result = remind_sellers_to_hand_over()
        assert 'Outside' in result
        push.assert_not_called()
        # ولا يُختم: المرور التالي داخل النافذة يلتقطه.
        paid_reservation.refresh_from_db()
        assert paid_reservation.handover_reminder_sent_at is None


class TestWhatsapp:
    def test_whatsapp_is_attempted_with_the_seller_number(
            self, settings, paid_reservation):
        settings.WEVLIX_TEMPLATE_HANDOVER_REMINDER = 'handover_v1'
        with patch('core.notifications.notify_push'), \
                patch('core.services.wevlix._post',
                      return_value={'messageId': 'm1'}) as post:
            run_inside_window()
        payload = post.call_args.args[1]
        assert payload['template'] == 'handover_v1'
        assert payload['to'] == paid_reservation.item.owner.phone

    def test_no_template_still_sends_the_push(self, settings, paid_reservation):
        # الميزة تعمل منقوصةً لا أن تتعطّل.
        settings.WEVLIX_TEMPLATE_HANDOVER_REMINDER = ''
        with patch('core.notifications.notify_push') as push:
            assert 'Sent 1' in run_inside_window()
        push.assert_called_once()

    def test_a_whatsapp_outage_does_not_lose_the_reminder(
            self, settings, paid_reservation):
        from core.services.wevlix import WevlixError

        settings.WEVLIX_TEMPLATE_HANDOVER_REMINDER = 'handover_v1'
        with patch('core.notifications.notify_push') as push, \
                patch('core.services.wevlix._post',
                      side_effect=WevlixError('down')):
            assert 'Sent 1' in run_inside_window()
        # الإشعار خرج، وواتساب تحسينٌ فوقه لا شرط له.
        push.assert_called_once()
