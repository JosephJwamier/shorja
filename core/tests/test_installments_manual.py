"""التقسيط اليدوي عبر كي كارد، وإصلاحات المراجعة على المسار الآلي.

المسار اليدوي (الافتراضي): المشتري يرسل رقم حسابه ← «سيتم الاتصال بك» ←
الإدارة تتحقّق وتتصل وتفعّل الخطة في منصة كي كارد ← توافق هنا فيكتمل الحجز
كحجز عادي. ما يهمّ اختباره: لا مال يتحرّك قبل الموافقة، والحجز لا ينتهي
وهو بانتظار المكالمة، ولا يُدفع مرّتين (تقسيط + محفظة)، ورقم الحساب لا
يخرج من لوحة الإدارة ويُمحى بعد القرار.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import InstallmentPlan, InstallmentReversal, Transaction, User
from core.services.installments.base import Reversal
from core.services.installments.cards import scrub_sentry_event
from core.services.installments.qicard import QiCardProvider
from core.services.installments.registry import reset_cache
from core.services.installments.service import (
    InstallmentNotAllowed, InstallmentReviewError, approve_request, confirm,
    live_plan_for, mark_reversal_done, options, quote, reject_request,
    request_manual, sync_plan,
)
from core.tasks import process_expired_reservations
from core.tests.test_installments import (
    GOOD_CARD, MONTHLY, PRICE, PROVIDER_SETTINGS, QI_AMOUNT, balance,
    make_reservation, make_user,
)
from core.throttling import InstallmentThrottle
from core.wallet_utils import refund_to_buyer

pytestmark = pytest.mark.django_db

ACCOUNT = '7001234567890123'

MANUAL_SETTINGS = {
    'qicard': {
        'MODE': 'manual',
        'MONTHS': 10,
        'CUSTOMER_FEE_PERCENT': '10',
        'MERCHANT_FEE_PERCENT': '5',
        'SETTLEMENT_MODE': 'upfront',
        'MIN_AMOUNT': '250000',
        'MAX_AMOUNT': '5000000',
    }
}


@pytest.fixture(autouse=True)
def manual(settings):
    settings.INSTALLMENT_PROVIDERS = MANUAL_SETTINGS
    settings.INSTALLMENT_PLATFORM_FEE_PERCENT = '2'
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def api_mode(settings):
    settings.INSTALLMENT_PROVIDERS = PROVIDER_SETTINGS
    settings.DEBUG = True
    reset_cache()


@pytest.fixture
def staff():
    return User.objects.create_superuser(username='ins_staff', password='x',
                                         email='staff@example.com')


def requested(buyer, seller, **extra):
    reservation = make_reservation(buyer, seller, **extra)
    plan = request_manual(user=buyer, reservation=reservation, account_number=ACCOUNT)
    return reservation, plan


# ---------------------------------------------------------------------------
# الطلب
# ---------------------------------------------------------------------------

def test_manual_mode_is_offered_without_any_qi_credentials():
    offer = options()
    assert offer is not None
    assert offer['mode'] == 'manual'


def test_manual_mode_can_be_switched_off(settings):
    settings.INSTALLMENT_PROVIDERS = {'qicard': {**MANUAL_SETTINGS['qicard'], 'ENABLED': '0'}}
    reset_cache()
    assert options() is None


def test_a_request_moves_no_money_and_keeps_the_reservation_pending():
    buyer, seller = make_user('m_req_b'), make_user('m_req_s')
    reservation, plan = requested(buyer, seller)

    assert plan.state == InstallmentPlan.STATE_REQUESTED
    assert plan.account_number == ACCOUNT
    assert plan.card_last4 == '0123'
    assert plan.principal == PRICE
    assert plan.qi_amount == QI_AMOUNT
    assert plan.monthly_amount == MONTHLY
    reservation.refresh_from_db()
    assert reservation.status == 'pending'
    assert balance(buyer) == Decimal('0.00')
    assert not Transaction.objects.filter(reservation=reservation).exists()


def test_asking_twice_queues_one_call_not_two():
    buyer, seller = make_user('m_twice_b'), make_user('m_twice_s')
    reservation, first = requested(buyer, seller)
    second = request_manual(user=buyer, reservation=reservation,
                            account_number='7009999999999')
    assert second.pk == first.pk
    assert second.account_number == '7009999999999'
    assert InstallmentPlan.objects.filter(reservation=reservation).count() == 1


def test_the_request_endpoint_never_echoes_the_account_number():
    buyer, seller = make_user('m_http_b'), make_user('m_http_s')
    reservation = make_reservation(buyer, seller)
    client = APIClient()
    client.force_authenticate(user=buyer)

    response = client.post('/api/installments/request/',
                           {'reservation_id': reservation.id,
                            'account_number': ACCOUNT}, format='json')
    assert response.status_code == 200, response.data
    assert 'سيتم الاتصال بك' in response.data['message']
    assert response.data['data']['state'] == 'requested'
    assert ACCOUNT not in str(response.data)
    assert 'card_holder_name' not in response.data['data']

    bad = client.post('/api/installments/request/',
                      {'reservation_id': reservation.id,
                       'account_number': '12ab'}, format='json')
    assert bad.data['eligible'] is False
    assert bad.data['code'] == 'invalid_account'
    assert '12ab' not in str(bad.data)


def test_the_order_card_says_the_request_is_in():
    from core.serializers import _reservation_installment
    buyer, seller = make_user('m_card_b'), make_user('m_card_s')
    reservation, plan = requested(buyer, seller)
    block = _reservation_installment(reservation)
    assert block['state'] == 'requested'
    assert block['reference'] == plan.reference


def test_a_reservation_waiting_for_the_call_does_not_expire():
    buyer, seller = make_user('m_exp_b'), make_user('m_exp_s')
    reservation, _ = requested(buyer, seller)
    reservation.expires_at = timezone.now() - timedelta(hours=1)
    reservation.save(update_fields=['expires_at'])

    process_expired_reservations()
    reservation.refresh_from_db()
    assert reservation.status == 'pending'


def test_the_wallet_cannot_pay_while_a_request_is_open():
    """وإلا دُفع الطلب مرّتين: من المحفظة، ومن راتبه عبر خطة تفعّلها الإدارة."""
    buyer = make_user('m_wal_b', balance=Decimal('5000000'))
    seller = make_user('m_wal_s')
    reservation, _ = requested(buyer, seller)
    client = APIClient()
    client.force_authenticate(user=buyer)

    response = client.post('/api/reservations/%d/complete/' % reservation.id,
                           {'payment_type': 'full'}, format='json')
    assert response.status_code == 400
    assert response.data['code'] == 'installment_requested'
    reservation.refresh_from_db()
    assert reservation.status == 'pending'


def test_the_buyer_can_withdraw_the_request():
    buyer, seller = make_user('m_wd_b'), make_user('m_wd_s')
    _, plan = requested(buyer, seller)
    client = APIClient()
    client.force_authenticate(user=buyer)

    response = client.post('/api/installments/%s/cancel/' % plan.reference)
    assert response.status_code == 200, response.data
    plan.refresh_from_db()
    assert plan.state == InstallmentPlan.STATE_CANCELLED
    assert plan.account_number == ''


def test_cancelling_the_reservation_withdraws_the_request():
    buyer, seller = make_user('m_rc_b'), make_user('m_rc_s')
    reservation, plan = requested(buyer, seller)
    client = APIClient()
    client.force_authenticate(user=buyer)

    response = client.post('/api/reservations/%d/cancel/' % reservation.id)
    assert response.status_code == 200, response.data
    plan.refresh_from_db()
    assert plan.state == InstallmentPlan.STATE_CANCELLED
    assert plan.account_number == ''


# ---------------------------------------------------------------------------
# قرار الإدارة
# ---------------------------------------------------------------------------

def test_approving_completes_the_reservation_like_any_paid_order(staff):
    buyer, seller = make_user('m_ok_b'), make_user('m_ok_s')
    reservation, plan = requested(buyer, seller)

    plan = approve_request(plan.pk, reviewed_by=staff, provider_reference='QI-778')

    reservation.refresh_from_db()
    assert reservation.status == 'completed'
    assert reservation.is_deposit_only is False
    assert plan.state == InstallmentPlan.STATE_ACTIVE
    assert plan.applied_at is not None
    assert plan.provider_plan_id == 'QI-778'
    assert plan.reviewed_by == staff
    assert plan.account_number == ''
    assert plan.card_last4 == '0123'
    # Credited and debited in the same breath: the buyer's wallet is untouched.
    assert balance(buyer) == Decimal('0.00')
    assert live_plan_for(reservation) == plan


def test_approving_twice_completes_once(staff):
    buyer, seller = make_user('m_dbl_b'), make_user('m_dbl_s')
    reservation, plan = requested(buyer, seller)
    stock = reservation.stock_variant.quantity

    approve_request(plan.pk, reviewed_by=staff, provider_reference='QI-1')
    approve_request(plan.pk, reviewed_by=staff, provider_reference='QI-1')

    reservation.refresh_from_db()
    assert reservation.stock_variant.quantity == stock - 1
    assert Transaction.objects.filter(reservation=reservation,
                                      transaction_type='installment').count() == 1


def test_approval_needs_qi_reference(staff):
    buyer, seller = make_user('m_ref_b'), make_user('m_ref_s')
    _, plan = requested(buyer, seller)
    with pytest.raises(InstallmentReviewError):
        approve_request(plan.pk, reviewed_by=staff, provider_reference='  ')
    plan.refresh_from_db()
    assert plan.state == InstallmentPlan.STATE_REQUESTED


def test_approval_works_after_the_reservation_clock_ran_out(staff):
    buyer, seller = make_user('m_late_b'), make_user('m_late_s')
    reservation, plan = requested(buyer, seller)
    reservation.expires_at = timezone.now() - timedelta(days=1)
    reservation.save(update_fields=['expires_at'])

    approve_request(plan.pk, reviewed_by=staff, provider_reference='QI-2')
    reservation.refresh_from_db()
    assert reservation.status == 'completed'


def test_approval_refuses_a_reservation_that_is_no_longer_pending(staff):
    buyer, seller = make_user('m_gone_b'), make_user('m_gone_s')
    reservation, plan = requested(buyer, seller)
    reservation.status = 'cancelled'
    reservation.save(update_fields=['status'])

    with pytest.raises(InstallmentReviewError):
        approve_request(plan.pk, reviewed_by=staff, provider_reference='QI-3')
    assert balance(buyer) == Decimal('0.00')


def test_approval_refuses_when_the_amount_moved(staff):
    buyer, seller = make_user('m_amt_b'), make_user('m_amt_s')
    reservation, plan = requested(buyer, seller)
    reservation.total_price = PRICE + 50000
    reservation.save(update_fields=['total_price'])

    with pytest.raises(InstallmentReviewError):
        approve_request(plan.pk, reviewed_by=staff, provider_reference='QI-4')
    reservation.refresh_from_db()
    assert reservation.status == 'pending'


def test_rejecting_needs_a_reason_and_gives_the_reservation_a_day(staff):
    buyer, seller = make_user('m_rej_b'), make_user('m_rej_s')
    reservation, plan = requested(buyer, seller)
    reservation.expires_at = timezone.now() - timedelta(hours=3)
    reservation.save(update_fields=['expires_at'])

    with pytest.raises(InstallmentReviewError):
        reject_request(plan.pk, reviewed_by=staff, notes='')

    plan = reject_request(plan.pk, reviewed_by=staff, notes='الحساب غير مؤهّل')
    assert plan.state == InstallmentPlan.STATE_REJECTED
    assert plan.failure_reason == 'الحساب غير مؤهّل'
    assert plan.account_number == ''
    reservation.refresh_from_db()
    assert reservation.status == 'pending'
    assert reservation.expires_at > timezone.now() + timedelta(hours=23)


def test_a_rejected_buyer_can_then_pay_from_the_wallet(staff):
    buyer = make_user('m_rw_b', balance=Decimal('5000000'))
    seller = make_user('m_rw_s')
    reservation, plan = requested(buyer, seller)
    reject_request(plan.pk, reviewed_by=staff, notes='لم يُجب على الهاتف')

    client = APIClient()
    client.force_authenticate(user=buyer)
    response = client.post('/api/reservations/%d/complete/' % reservation.id,
                           {'payment_type': 'full'}, format='json')
    assert response.status_code == 200, response.data


# ---------------------------------------------------------------------------
# بعد الموافقة: الإلغاء يعود إلى كي كارد يدوياً
# ---------------------------------------------------------------------------

def test_cancelling_an_approved_order_queues_a_manual_reversal(staff):
    buyer, seller = make_user('m_cx_b'), make_user('m_cx_s')
    reservation, plan = requested(buyer, seller)
    approve_request(plan.pk, reviewed_by=staff, provider_reference='QI-5')

    with patch.object(QiCardProvider, 'reverse_plan') as reverse:
        client = APIClient()
        client.force_authenticate(user=buyer)
        response = client.post('/api/reservations/%d/cancel/' % reservation.id)
        assert response.status_code == 200, response.data
        reverse.assert_not_called()  # no API: a person cancels it on Qi's platform

    assert balance(buyer) == Decimal('0.00')
    reversal = InstallmentReversal.objects.get(plan=plan)
    assert reversal.state == InstallmentReversal.STATE_PENDING
    assert reversal.amount == QI_AMOUNT

    mark_reversal_done(reversal.pk, reviewed_by=staff, provider_reference='QI-5-X')
    plan.refresh_from_db()
    reversal.refresh_from_db()
    assert reversal.state == InstallmentReversal.STATE_CONFIRMED
    assert plan.state == InstallmentPlan.STATE_REVERSED
    assert plan.reversed_amount == QI_AMOUNT


def test_a_manual_plan_cannot_be_synced(staff):
    from core.services.installments.base import InstallmentError
    buyer, seller = make_user('m_sync_b'), make_user('m_sync_s')
    _, plan = requested(buyer, seller)
    plan = approve_request(plan.pk, reviewed_by=staff, provider_reference='QI-6')
    with pytest.raises(InstallmentError):
        sync_plan(plan)


# ---------------------------------------------------------------------------
# صفحة المراجعة في لوحة الإدارة
# ---------------------------------------------------------------------------

def test_the_review_page_shows_the_number_and_approves(staff):
    buyer, seller = make_user('m_adm_b'), make_user('m_adm_s')
    reservation, plan = requested(buyer, seller)
    client = Client()
    client.force_login(staff)
    url = reverse('admin:core_installmentplan_review', args=[plan.pk])

    page = client.get(url)
    assert page.status_code == 200
    assert ACCOUNT in page.content.decode()
    # The figure the admin types into Qi is the grossed-up one, not the price,
    # and it is not localised into "1073685,00".
    content = page.content.decode()
    assert '>1074000<' in content
    assert '1074000,00' not in content

    done = client.post(url, {'decision': 'approve', 'provider_reference': 'QI-9'})
    assert done.status_code == 302
    reservation.refresh_from_db()
    assert reservation.status == 'completed'

    after = client.get(url).content.decode()
    assert ACCOUNT not in after
    assert 'QI-9' in after


def test_the_review_page_breaks_the_amount_down_and_it_adds_up(staff):
    """المراجع يرى الدينار داخلاً وخارجاً: ثمن الجهاز والتوصيل وعمولة
    المنصة وعمولة كي كارد والمجموع — ويجمع العمودان إلى نفس الرقم.

    الاختبار على المعادلة لا على النصّ: أي بند يُنسى من الصفحة أو يُحسب
    مرّتين يكسر الجمع."""
    buyer, seller = make_user('m_brk_b'), make_user('m_brk_s')
    reservation, plan = requested(buyer, seller)
    client = Client()
    client.force_login(staff)
    page = client.get(reverse('admin:core_installmentplan_review', args=[plan.pk]))
    content = page.content.decode()

    # كل بند مطلوب، بقيمته المجمَّدة على الخطة.
    for value in (reservation.total_price, plan.principal,
                  plan.platform_fee_amount, plan.merchant_fee_amount,
                  plan.customer_fee_amount, plan.total_payable):
        assert str(value) in content, value
    assert str(plan.merchant_fee_amount + plan.customer_fee_amount) in content

    # تكوين المبلغ: الثمن + التوصيل + العمولتان + التدوير = ما يدفعه الزبون.
    financed_fee = plan.principal - reservation.total_price
    build_up = (reservation.total_price + financed_fee
                + plan.platform_fee_amount + plan.merchant_fee_amount
                + plan.customer_fee_amount)
    rounding = (plan.amount_at_qi - plan.principal
                - plan.platform_fee_amount - plan.merchant_fee_amount)
    assert build_up + rounding == plan.total_payable

    # التوزيع: البائع + عمولة البيع + التوصيل + عمولة التقسيط + كي كارد.
    seller_net = reservation.total_price - reservation.platform_commission_amount
    payout = (seller_net + reservation.platform_commission_amount + financed_fee
              + plan.platform_fee_amount
              + plan.merchant_fee_amount + plan.customer_fee_amount + rounding)
    assert payout == plan.total_payable


def test_the_changelist_never_shows_the_full_number(staff):
    buyer, seller = make_user('m_cl_b'), make_user('m_cl_s')
    _, plan = requested(buyer, seller)
    client = Client()
    client.force_login(staff)
    listing = client.get(reverse('admin:core_installmentplan_changelist')).content.decode()
    assert ACCOUNT not in listing
    detail = client.get(reverse('admin:core_installmentplan_change', args=[plan.pk])).content.decode()
    assert ACCOUNT not in detail


def test_sentry_never_receives_the_account_number():
    event = {'request': {'data': {'account_number': ACCOUNT, 'reservation_id': 1}}}
    scrubbed = scrub_sentry_event(event)
    assert scrubbed['request']['data']['account_number'] == '[redacted]'
    assert scrubbed['request']['data']['reservation_id'] == 1


# ---------------------------------------------------------------------------
# إصلاحات المراجعة على المسار الآلي
# ---------------------------------------------------------------------------

def test_racing_confirms_create_one_plan_at_qi(api_mode):
    """طلبان متزامنان كانا يُنشئان خطّتين عند كي كارد، والثانية لا تُلغى."""
    buyer, seller = make_user('fx_race_b'), make_user('fx_race_s')
    plan = quote(user=buyer, reservation=make_reservation(buyer, seller),
                 card_number=GOOD_CARD)
    real = QiCardProvider.create_plan
    calls = []

    def create(self, *, plan, card_number=None):
        calls.append(1)
        if len(calls) == 1:
            # The second request lands while the first waits on Qi.
            with pytest.raises(InstallmentNotAllowed) as exc:
                confirm(user=buyer, reference=plan.reference)
            assert exc.value.code == 'in_progress'
        return real(self, plan=plan, card_number=card_number)

    with patch.object(QiCardProvider, 'create_plan', create):
        result = confirm(user=buyer, reference=plan.reference)

    assert len(calls) == 1
    assert result.state == InstallmentPlan.STATE_ACTIVE


def test_a_failed_create_call_gives_the_quote_back(api_mode):
    from core.services.installments.base import InstallmentError
    buyer, seller = make_user('fx_back_b'), make_user('fx_back_s')
    plan = quote(user=buyer, reservation=make_reservation(buyer, seller),
                 card_number=GOOD_CARD)
    with patch.object(QiCardProvider, 'create_plan',
                      side_effect=InstallmentError('timeout')):
        with pytest.raises(InstallmentError):
            confirm(user=buyer, reference=plan.reference)
    plan.refresh_from_db()
    assert plan.state == InstallmentPlan.STATE_QUOTED
    assert confirm(user=buyer, reference=plan.reference).state == InstallmentPlan.STATE_ACTIVE


def test_a_webhook_cannot_revive_a_failed_plan(api_mode):
    buyer, seller = make_user('fx_rev_b'), make_user('fx_rev_s')
    reservation = make_reservation(buyer, seller)
    plan = quote(user=buyer, reservation=reservation, card_number=GOOD_CARD)
    with patch('core.wallet_utils.complete_reservation', side_effect=ValueError('boom')), \
         patch.object(QiCardProvider, 'reverse_plan',
                      lambda self, **kw: Reversal(accepted=False, failure_reason='no')):
        with pytest.raises(ValueError):
            confirm(user=buyer, reference=plan.reference)
    plan.refresh_from_db()
    assert plan.state == InstallmentPlan.STATE_FAILED

    sync_plan(plan)
    plan.refresh_from_db()
    assert plan.state == InstallmentPlan.STATE_FAILED
    assert live_plan_for(reservation) is None


def test_pending_reversals_count_against_what_is_left(api_mode):
    buyer, seller = make_user('fx_out_b'), make_user('fx_out_s')
    reservation = make_reservation(buyer, seller)
    plan = quote(user=buyer, reservation=reservation, card_number=GOOD_CARD)
    plan = confirm(user=buyer, reference=plan.reference)
    reservation.refresh_from_db()

    with transaction.atomic():  # the reversals stay pending: on_commit never fires here
        refund_to_buyer(reservation, Decimal('600000'))
        refund_to_buyer(reservation, Decimal('600000'))

    total = sum(r.amount for r in InstallmentReversal.objects.filter(plan=plan))
    assert total == plan.amount_at_qi


def test_the_check_does_not_reveal_who_owns_a_card(api_mode):
    buyer, seller = make_user('fx_name_b'), make_user('fx_name_s')
    reservation = make_reservation(buyer, seller)
    client = APIClient()
    client.force_authenticate(user=buyer)
    response = client.post('/api/installments/check/',
                           {'reservation_id': reservation.id,
                            'card_number': GOOD_CARD}, format='json')
    assert response.status_code == 200, response.data
    assert 'card_holder_name' not in response.data['data']
    assert 'حامل' not in str(response.data)


def test_card_and_account_checks_are_rate_limited(api_mode):
    buyer, seller = make_user('fx_thr_b'), make_user('fx_thr_s')
    reservation = make_reservation(buyer, seller)
    client = APIClient()
    client.force_authenticate(user=buyer)
    with patch.object(InstallmentThrottle, 'THROTTLE_RATES', {'installments': '2/hour'}):
        codes = [client.post('/api/installments/check/',
                             {'reservation_id': reservation.id,
                              'card_number': GOOD_CARD}, format='json').status_code
                 for _ in range(3)]
    assert codes == [200, 200, 429]
