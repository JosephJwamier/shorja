"""التقسيط عبر كي كارد.

نداءات المموّل كلها إما sandbox (أجوبة محلّية ثابتة) أو مُرقَّعة عند حدّ
المزوّد — لا شبكة هنا.

الحالات التي تهمّ ليست المسار السعيد. هي: محفظة تُقيَّد ولا تُخصم (مال من
لا شيء)، تأكيدٌ يُنفَّذ مرّتين (تمويل مزدوج على طلب واحد)، وإلغاء حجز
مموَّل (المال يعود للمموّل لا للمشتري — وإلّا قبض المشتري ثمن البضاعة
نقداً والاستقطاع من راتبه مستمرّ). وفوقها واحدة لا تُرى بالعين: رقم
البطاقة لا يُخزَّن.
"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    Category, InstallmentCollection, InstallmentPlan, InstallmentReversal, Item,
    Location, Reservation, Transaction, User, get_or_create_wallet,
)
from core.services.installments.base import InstallmentError, NotEligible
from core.services.installments.cards import luhn_ok, validate, InvalidCardNumber
from core.services.installments.registry import get_provider, reset_cache
from core.services.installments.service import (
    confirm, execute_reversal, options, price_preview, quote, sync_plan,
)
from core.wallet_utils import release_reservation_payment

pytestmark = pytest.mark.django_db

PROVIDER_SETTINGS = {
    'qicard': {
        'BASE_URL': 'https://api-test.qi.example/v1',
        'MERCHANT_ID': 'shorjeh',
        'API_KEY': 'test_key',
        'MONTHS': 10,
        'CUSTOMER_FEE_PERCENT': '10',
        'MERCHANT_FEE_PERCENT': '5',
        'SETTLEMENT_MODE': 'upfront',
        'MIN_AMOUNT': '250000',
        'MAX_AMOUNT': '5000000',
        'SANDBOX': '1',
    }
}

PRICE = Decimal('1000000.00')


@pytest.fixture(autouse=True)
def configured(settings):
    settings.INSTALLMENT_PROVIDERS = PROVIDER_SETTINGS
    settings.INSTALLMENT_SELLER_FEE_SHARE = '1'
    # المزوّد التجريبي مربوط بـ DEBUG عن قصد (QiCardProvider.is_configured):
    # ليستحيل أن يبقى مفتوحاً في الإنتاج سهواً.
    settings.DEBUG = True
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def client():
    return APIClient()


def card_ending_in(digit):
    """رقم بطاقة من 16 خانة صحيح الـ Luhn وآخره `digit`.

    المموّل التجريبي يبني جوابه على آخر رقم (0 يرفض، 9 يضع سقفاً منخفضاً)،
    وآخر رقم في أي PAN هو خانة التحقّق — فلا يمكن اختياره، يُبحث عنه.
    """
    for n in range(1000):
        candidate = '4' + str(n).zfill(14) + str(digit)
        if luhn_ok(candidate):
            return candidate
    raise AssertionError('no Luhn-valid candidate ending in %s' % digit)


GOOD_CARD = card_ending_in(1)
REFUSED_CARD = card_ending_in(0)
LOW_LIMIT_CARD = card_ending_in(9)


def make_user(username, balance=Decimal('0.00')):
    user = User.objects.create_user(username=username, password='pass12345')
    if balance:
        get_or_create_wallet(user).credit(balance)
    return user


def make_item(owner):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return Item.objects.create(owner=owner, title='تلفزيون', description='d',
                               category=category, condition='new', quantity=2,
                               status='active')


def make_reservation(buyer, seller, total_price=PRICE, **extra):
    location = Location.objects.create(
        user=buyer, name='البيت', address_line1='شارع', city='بغداد',
        state='بغداد', country='العراق', contact_person='م',
        contact_phone='07701234567')
    kwargs = dict(
        item=make_item(seller), buyer=buyer, quantity=1,
        deposit_amount=Decimal('0.00'), total_price=total_price,
        status='pending', expires_at=timezone.now() + timedelta(days=1),
        delivery_location=location,
    )
    kwargs.update(extra)
    return Reservation.objects.create(**kwargs)


def balance(user):
    return get_or_create_wallet(user).balance


def financed(buyer, seller, card=GOOD_CARD, **extra):
    """حجز مموَّل مكتمل — نقطة البداية لاختبارات ما بعد الشراء."""
    reservation = make_reservation(buyer, seller, **extra)
    plan = quote(user=buyer, reservation=reservation, card_number=card)
    plan = confirm(user=buyer, reference=plan.reference)
    reservation.refresh_from_db()
    return reservation, plan


# ---------------------------------------------------------------------------
# ما يُعرض قبل أي بطاقة
# ---------------------------------------------------------------------------

def test_options_are_hidden_when_no_financier_is_configured(settings):
    settings.INSTALLMENT_PROVIDERS = {}
    reset_cache()
    assert options() is None


def test_sandbox_is_not_configured_outside_debug(settings):
    settings.DEBUG = False
    reset_cache()
    assert get_provider('qicard').is_configured is False
    assert options() is None


def test_options_carry_the_negotiated_terms():
    data = options()
    assert data['months'] == 10
    assert data['customer_fee_percent'] == '10'
    assert data['settlement_mode'] == 'upfront'


def test_price_preview_is_the_monthly_figure():
    preview = price_preview(PRICE)
    # 10% من المشتري فوق السعر، مقسّمة على عشرة أشهر.
    assert preview['total_payable'] == '1100000.00'
    assert preview['monthly_amount'] == '110000'


def test_price_preview_is_none_below_the_floor():
    assert price_preview(Decimal('100000')) is None


# ---------------------------------------------------------------------------
# الفحص
# ---------------------------------------------------------------------------

def test_a_quote_prices_the_whole_schedule():
    buyer, seller = make_user('ins_q_buyer'), make_user('ins_q_seller')
    plan = quote(user=buyer, reservation=make_reservation(buyer, seller),
                 card_number=GOOD_CARD)

    assert plan.state == InstallmentPlan.STATE_QUOTED
    assert plan.principal == PRICE
    assert plan.customer_fee_amount == Decimal('100000.00')
    assert plan.total_payable == Decimal('1100000.00')
    assert plan.monthly_amount == Decimal('110000')
    assert plan.merchant_fee_amount == Decimal('50000.00')
    assert plan.seller_fee_amount == Decimal('50000.00')
    assert plan.platform_settlement == Decimal('950000.00')
    assert plan.applied_at is None


def test_the_card_number_is_never_stored():
    """الحجّة الكاملة في services/installments/cards.py: الرقم يعبر مرّة."""
    buyer, seller = make_user('ins_pci_buyer'), make_user('ins_pci_seller')
    plan = quote(user=buyer, reservation=make_reservation(buyer, seller),
                 card_number=GOOD_CARD)

    stored = ' '.join(
        str(getattr(plan, field.name)) for field in InstallmentPlan._meta.fields
    )
    assert GOOD_CARD not in stored
    assert plan.card_last4 == GOOD_CARD[-4:]


def test_a_refused_card_raises_not_eligible():
    buyer, seller = make_user('ins_ref_buyer'), make_user('ins_ref_seller')
    with pytest.raises(NotEligible):
        quote(user=buyer, reservation=make_reservation(buyer, seller),
              card_number=REFUSED_CARD)


def test_a_card_whose_limit_is_too_low_is_refused():
    buyer, seller = make_user('ins_lim_buyer'), make_user('ins_lim_seller')
    with pytest.raises(NotEligible):
        quote(user=buyer, reservation=make_reservation(buyer, seller),
              card_number=LOW_LIMIT_CARD)


def test_a_mistyped_card_is_caught_before_the_financier_is_asked():
    with pytest.raises(InvalidCardNumber):
        validate('4111 1111 1111 1112')


def test_spaces_between_the_groups_are_accepted():
    grouped = ' '.join([GOOD_CARD[i:i + 4] for i in range(0, 16, 4)])
    assert validate(grouped) == GOOD_CARD


def test_a_second_quote_expires_the_first():
    buyer, seller = make_user('ins_two_buyer'), make_user('ins_two_seller')
    reservation = make_reservation(buyer, seller)
    first = quote(user=buyer, reservation=reservation, card_number=GOOD_CARD)
    quote(user=buyer, reservation=reservation, card_number=GOOD_CARD)

    first.refresh_from_db()
    assert first.state == InstallmentPlan.STATE_EXPIRED


# ---------------------------------------------------------------------------
# التأكيد — المكان الوحيد الذي يتحرّك فيه المال
# ---------------------------------------------------------------------------

def test_confirming_completes_the_reservation_and_holds_the_money():
    buyer, seller = make_user('ins_c_buyer'), make_user('ins_c_seller')
    reservation, plan = financed(buyer, seller)

    assert plan.state == InstallmentPlan.STATE_ACTIVE
    assert plan.applied_at is not None
    assert reservation.status == 'completed'
    assert reservation.is_deposit_only is False
    # المال في الضمان كأي حجز مدفوع: لا شيء وصل البائع بعد.
    assert reservation.amount_charged == PRICE
    assert reservation.payment_released_to_seller is False
    assert balance(seller) == Decimal('0.00')


def test_the_buyer_pays_nothing_from_their_own_wallet():
    """القيد ثم الخصم في معاملة واحدة: صافي أثر المحفظة صفر، ولا لحظة
    يستطيع فيها المشتري إنفاق مال التمويل على شيء آخر."""
    buyer, seller = make_user('ins_w_buyer'), make_user('ins_w_seller')
    financed(buyer, seller)

    assert balance(buyer) == Decimal('0.00')
    kinds = dict(Transaction.objects.filter(user=buyer)
                 .values_list('transaction_type', 'amount'))
    assert kinds['installment'] == PRICE
    assert kinds['purchase'] == PRICE


def test_a_buyer_with_an_empty_wallet_can_buy_on_installments():
    """جوهر الميزة: صاحب الراتب لا يملك الثمن اليوم."""
    buyer, seller = make_user('ins_poor_buyer'), make_user('ins_poor_seller')
    assert balance(buyer) == Decimal('0.00')
    reservation, _ = financed(buyer, seller)
    assert reservation.status == 'completed'


def test_confirming_twice_finances_once():
    buyer, seller = make_user('ins_d_buyer'), make_user('ins_d_seller')
    reservation, plan = financed(buyer, seller)
    again = confirm(user=buyer, reference=plan.reference)

    assert again.applied_at == plan.applied_at
    assert Transaction.objects.filter(user=buyer,
                                      transaction_type='installment').count() == 1
    assert balance(buyer) == Decimal('0.00')


def test_the_stock_is_taken_once():
    buyer, seller = make_user('ins_s_buyer'), make_user('ins_s_seller')
    reservation, plan = financed(buyer, seller)
    confirm(user=buyer, reference=plan.reference)

    reservation.item.refresh_from_db()
    assert reservation.item.sold_count == 1


def test_an_expired_quote_is_refused():
    buyer, seller = make_user('ins_e_buyer'), make_user('ins_e_seller')
    plan = quote(user=buyer, reservation=make_reservation(buyer, seller),
                 card_number=GOOD_CARD)
    InstallmentPlan.objects.filter(pk=plan.pk).update(
        expires_at=timezone.now() - timedelta(minutes=1))

    from core.services.installments.service import InstallmentNotAllowed
    with pytest.raises(InstallmentNotAllowed) as exc:
        confirm(user=buyer, reference=plan.reference)
    assert exc.value.code == 'stale_quote'


def test_a_plan_created_at_the_financier_is_reversed_if_the_order_fails():
    """الحالة المكلفة: الخطة قائمة عند كي كارد والطلب لم يتمّ — عشرة أشهر
    استقطاع مقابل لا شيء."""
    buyer, seller = make_user('ins_orph_buyer'), make_user('ins_orph_seller')
    plan = quote(user=buyer, reservation=make_reservation(buyer, seller),
                 card_number=GOOD_CARD)

    with patch('core.wallet_utils.complete_reservation',
               side_effect=ValueError('boom')):
        with pytest.raises(ValueError):
            confirm(user=buyer, reference=plan.reference)

    plan.refresh_from_db()
    # الحالة النهائية `reversed` لا `failed`: المموّل قَبِل الإرجاع كاملاً،
    # فلا شيء مستحقّ على المشتري. المهمّ أنها ليست قائمة، وأن السبب مكتوب.
    assert plan.is_live is False
    assert plan.failure_reason
    reversal = InstallmentReversal.objects.get(plan=plan)
    assert reversal.amount == PRICE
    assert reversal.state == InstallmentReversal.STATE_CONFIRMED
    assert plan.reversed_amount == PRICE


def test_an_auction_win_cannot_be_financed():
    """عربون المزايدة خُصم من المحفظة أصلاً؛ تمويل الباقي يشطر الشراء بين
    مموّلين ويجعل حساب الاسترداد ملتبساً."""
    from core.models import AuctionListing
    buyer, seller = make_user('ins_a_buyer'), make_user('ins_a_seller')
    item = make_item(seller)
    auction = AuctionListing.objects.create(
        item=item, starting_price=PRICE, current_price=PRICE,
        minimum_increment=Decimal('10000'), bid_deposit=Decimal('50000'),
        start_time=timezone.now() - timedelta(hours=2),
        end_time=timezone.now() - timedelta(minutes=1), status='ended')
    reservation = make_reservation(buyer, seller, item=item, auction=auction)

    from core.services.installments.service import InstallmentNotAllowed
    with pytest.raises(InstallmentNotAllowed) as exc:
        quote(user=buyer, reservation=reservation, card_number=GOOD_CARD)
    assert exc.value.code == 'auction'


def test_a_small_order_is_refused():
    buyer, seller = make_user('ins_min_buyer'), make_user('ins_min_seller')
    reservation = make_reservation(buyer, seller, total_price=Decimal('100000'))

    from core.services.installments.service import InstallmentNotAllowed
    with pytest.raises(InstallmentNotAllowed) as exc:
        quote(user=buyer, reservation=reservation, card_number=GOOD_CARD)
    assert exc.value.code == 'below_min'


# ---------------------------------------------------------------------------
# من يحمل رسم المموّل
# ---------------------------------------------------------------------------

def test_the_seller_carries_the_financier_fee_at_release():
    buyer, seller = make_user('ins_f_buyer'), make_user('ins_f_seller')
    reservation, plan = financed(buyer, seller)

    from core.services.commission import platform_account
    release_reservation_payment(reservation)

    # 1,000,000 ناقص 5% رسم تقسيط = 950,000 للبائع، والرسم لحساب المنصة
    # لأنها هي من دفعته (خُصم من تسويتها مع كي كارد).
    assert balance(seller) == Decimal('950000.00')
    assert balance(platform_account()) == Decimal('50000.00')


def test_the_platform_can_absorb_part_of_the_fee(settings):
    settings.INSTALLMENT_SELLER_FEE_SHARE = '0.5'
    reset_cache()
    buyer, seller = make_user('ins_sh_buyer'), make_user('ins_sh_seller')
    reservation, plan = financed(buyer, seller)

    assert plan.seller_fee_amount == Decimal('25000.00')
    release_reservation_payment(reservation)
    assert balance(seller) == Decimal('975000.00')


def test_a_wallet_sale_pays_no_installment_fee():
    """الحارس على الانحدار: الخصم الجديد لا يجوز أن يلمس بيعة غير مقسّطة."""
    from core.wallet_utils import complete_reservation
    buyer = make_user('ins_nf_buyer', balance=PRICE)
    seller = make_user('ins_nf_seller')
    reservation = make_reservation(buyer, seller)
    complete_reservation(reservation, 'full')
    release_reservation_payment(reservation)

    assert balance(seller) == PRICE


def test_the_monthly_mode_charges_the_merchant_nothing(settings):
    settings.INSTALLMENT_PROVIDERS = {
        'qicard': dict(PROVIDER_SETTINGS['qicard'], SETTLEMENT_MODE='monthly')}
    reset_cache()
    buyer, seller = make_user('ins_m_buyer'), make_user('ins_m_seller')
    reservation, plan = financed(buyer, seller)

    assert plan.settlement_mode == InstallmentPlan.SETTLEMENT_MONTHLY
    assert plan.merchant_fee_amount == Decimal('0.00')
    assert plan.seller_fee_amount == Decimal('0.00')
    release_reservation_payment(reservation)
    assert balance(seller) == PRICE


# ---------------------------------------------------------------------------
# ما حصّله المموّل فعلاً
# ---------------------------------------------------------------------------

def collected_plan(buyer, seller, months_ago=2):
    reservation, plan = financed(buyer, seller)
    InstallmentPlan.objects.filter(pk=plan.pk).update(
        applied_at=timezone.now() - timedelta(days=30 * months_ago))
    plan.refresh_from_db()
    return reservation, sync_plan(plan)


def test_syncing_records_what_the_financier_took():
    buyer, seller = make_user('ins_sy_buyer'), make_user('ins_sy_seller')
    _, plan = collected_plan(buyer, seller, months_ago=2)

    assert plan.collections.count() == 2
    assert plan.collected_amount == Decimal('220000.00')
    assert plan.state == InstallmentPlan.STATE_ACTIVE


def test_syncing_twice_records_each_installment_once():
    """الـ webhook يُعاد إرساله، وقيد القسط مرّتين يُضخّم المحصَّل — وهو
    الرقم الذي يُحسب منه ما يُعاد للمشتري."""
    buyer, seller = make_user('ins_sy2_buyer'), make_user('ins_sy2_seller')
    _, plan = collected_plan(buyer, seller, months_ago=3)
    sync_plan(plan)
    sync_plan(plan)

    assert InstallmentCollection.objects.filter(plan=plan).count() == 3
    plan.refresh_from_db()
    assert plan.collected_amount == Decimal('330000.00')


def test_a_fully_collected_plan_is_settled():
    buyer, seller = make_user('ins_set_buyer'), make_user('ins_set_seller')
    _, plan = collected_plan(buyer, seller, months_ago=11)
    assert plan.state == InstallmentPlan.STATE_SETTLED


# ---------------------------------------------------------------------------
# الإلغاء — أخطر جزء في الميزة
# ---------------------------------------------------------------------------

def test_cancelling_a_financed_order_does_not_pay_the_buyer(client):
    """المال لم يأتِ من محفظته: إيداعه فيها يعني أنه قبض ثمن البضاعة نقداً
    وكي كارد ما زالت تستقطع من راتبه."""
    buyer, seller = make_user('ins_x_buyer'), make_user('ins_x_seller')
    reservation, plan = financed(buyer, seller)

    client.force_authenticate(user=buyer)
    response = client.post('/api/reservations/%d/cancel/' % reservation.id)
    assert response.status_code == 200, response.data

    assert balance(buyer) == Decimal('0.00')
    assert not Transaction.objects.filter(user=buyer,
                                          transaction_type='refund').exists()
    reversal = InstallmentReversal.objects.get(plan=plan)
    assert reversal.amount == PRICE
    assert reversal.state == InstallmentReversal.STATE_PENDING


def test_the_reversal_closes_the_plan_once_the_financier_accepts():
    buyer, seller = make_user('ins_rv_buyer'), make_user('ins_rv_seller')
    reservation, plan = financed(buyer, seller)
    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post('/api/reservations/%d/cancel/' % reservation.id)

    reversal = InstallmentReversal.objects.get(plan=plan)
    execute_reversal(reversal.pk)

    reversal.refresh_from_db()
    plan.refresh_from_db()
    assert reversal.state == InstallmentReversal.STATE_CONFIRMED
    assert plan.state == InstallmentPlan.STATE_REVERSED
    assert plan.reversed_amount == PRICE


def test_a_refused_reversal_waits_for_a_human_instead_of_paying_the_buyer():
    buyer, seller = make_user('ins_rf_buyer'), make_user('ins_rf_seller')
    reservation, plan = financed(buyer, seller)
    client = APIClient()
    client.force_authenticate(user=buyer)
    client.post('/api/reservations/%d/cancel/' % reservation.id)
    reversal = InstallmentReversal.objects.get(plan=plan)

    from core.services.installments.base import Reversal as ReversalResult
    with patch.object(type(get_provider('qicard')), 'reverse_plan',
                      return_value=ReversalResult(accepted=False,
                                                  failure_reason='plan too old')):
        execute_reversal(reversal.pk)

    reversal.refresh_from_db()
    plan.refresh_from_db()
    assert reversal.state == InstallmentReversal.STATE_FAILED
    assert reversal.failure_reason == 'plan too old'
    # لا مال اختُرع للمشتري، ولا خطّة أُعلنت مُرجَعة وهي لم تُرجَع.
    assert balance(buyer) == Decimal('0.00')
    assert plan.state == InstallmentPlan.STATE_ACTIVE
    assert plan.reversed_amount == Decimal('0.00')


def test_what_the_buyer_already_paid_comes_back_to_them(client):
    """الاستثناء الوحيد: الأقساط التي استُقطعت منه فعلاً ووصلت حساب المنصة
    مالُه هو، فتعود إلى محفظته — والباقي فقط يعود للمموّل."""
    buyer, seller = make_user('ins_cb_buyer'), make_user('ins_cb_seller')
    reservation, plan = collected_plan(buyer, seller, months_ago=2)

    client.force_authenticate(user=buyer)
    response = client.post('/api/reservations/%d/cancel/' % reservation.id)
    assert response.status_code == 200, response.data

    assert balance(buyer) == Decimal('220000.00')
    reversal = InstallmentReversal.objects.get(plan=plan)
    assert reversal.amount == PRICE - Decimal('220000.00')


def test_two_refunds_never_return_the_collected_amount_twice():
    """استرداد جزئي ثم إلغاء: بلا عدّاد لما أُعيد، كل استرداد يدفع كامل
    المحصَّل من جديد."""
    from core.wallet_utils import refund_to_buyer
    from django.db import transaction as db_transaction

    buyer, seller = make_user('ins_t_buyer'), make_user('ins_t_seller')
    reservation, plan = collected_plan(buyer, seller, months_ago=2)

    with db_transaction.atomic():
        refund_to_buyer(reservation, Decimal('150000.00'))
    with db_transaction.atomic():
        refund_to_buyer(reservation, Decimal('150000.00'))

    # 220,000 محصَّلة: 150,000 ثم 70,000 فقط، والباقي إرجاع للمموّل.
    assert balance(buyer) == Decimal('220000.00')
    plan.refresh_from_db()
    assert plan.buyer_refunded_amount == Decimal('220000.00')


def test_a_wallet_order_still_refunds_to_the_wallet(client):
    """الحارس على الانحدار في الاتجاه الآخر: الموجّه الجديد لا يجوز أن
    يغيّر مسار الاسترداد العادي."""
    from core.wallet_utils import complete_reservation
    buyer = make_user('ins_wr_buyer', balance=PRICE)
    seller = make_user('ins_wr_seller')
    reservation = make_reservation(buyer, seller)
    complete_reservation(reservation, 'full')

    client.force_authenticate(user=buyer)
    assert client.post('/api/reservations/%d/cancel/'
                       % reservation.id).status_code == 200
    assert balance(buyer) == PRICE
    assert not InstallmentReversal.objects.exists()


# ---------------------------------------------------------------------------
# السطح الخارجي
# ---------------------------------------------------------------------------

def test_check_then_confirm_over_http(client):
    buyer, seller = make_user('ins_h_buyer'), make_user('ins_h_seller')
    reservation = make_reservation(buyer, seller)
    client.force_authenticate(user=buyer)

    check = client.post('/api/installments/check/',
                        {'reservation_id': reservation.id,
                         'card_number': GOOD_CARD}, format='json')
    assert check.status_code == 200, check.data
    assert check.data['eligible'] is True
    assert check.data['data']['monthly_amount'] == '110000.00'

    reference = check.data['data']['reference']
    done = client.post('/api/installments/confirm/', {'reference': reference},
                       format='json')
    assert done.status_code == 200, done.data
    reservation.refresh_from_db()
    assert reservation.status == 'completed'


def test_a_refusal_is_an_answer_not_an_error(client):
    """200 مع eligible=false: الرفض سبب يُعرض قرب حقل البطاقة، لا خطأ."""
    buyer, seller = make_user('ins_hr_buyer'), make_user('ins_hr_seller')
    reservation = make_reservation(buyer, seller)
    client.force_authenticate(user=buyer)

    response = client.post('/api/installments/check/',
                           {'reservation_id': reservation.id,
                            'card_number': REFUSED_CARD}, format='json')
    assert response.status_code == 200
    assert response.data['eligible'] is False
    assert response.data['message']


def test_a_financier_fault_is_a_bad_gateway_not_a_refusal(client):
    buyer, seller = make_user('ins_hf_buyer'), make_user('ins_hf_seller')
    reservation = make_reservation(buyer, seller)
    client.force_authenticate(user=buyer)

    with patch.object(type(get_provider('qicard')), 'check_eligibility',
                      side_effect=InstallmentError('timeout')):
        response = client.post('/api/installments/check/',
                               {'reservation_id': reservation.id,
                                'card_number': GOOD_CARD}, format='json')
    assert response.status_code == 502


def test_nobody_can_quote_against_someone_elses_reservation(client):
    buyer, seller = make_user('ins_o_buyer'), make_user('ins_o_seller')
    intruder = make_user('ins_o_intruder')
    reservation = make_reservation(buyer, seller)
    client.force_authenticate(user=intruder)

    response = client.post('/api/installments/check/',
                           {'reservation_id': reservation.id,
                            'card_number': GOOD_CARD}, format='json')
    assert response.status_code == 404


def test_confirming_someone_elses_quote_is_not_found(client):
    buyer, seller = make_user('ins_oc_buyer'), make_user('ins_oc_seller')
    intruder = make_user('ins_oc_intruder')
    plan = quote(user=buyer, reservation=make_reservation(buyer, seller),
                 card_number=GOOD_CARD)
    client.force_authenticate(user=intruder)

    response = client.post('/api/installments/confirm/',
                           {'reference': plan.reference}, format='json')
    assert response.status_code == 404


def test_a_validation_error_never_echoes_the_card_number(client):
    buyer = make_user('ins_v_buyer')
    client.force_authenticate(user=buyer)
    response = client.post('/api/installments/check/',
                           {'card_number': GOOD_CARD}, format='json')
    assert response.status_code == 400
    assert GOOD_CARD not in str(response.data)


def test_the_reservation_payload_shows_the_financing():
    buyer, seller = make_user('ins_ser_buyer'), make_user('ins_ser_seller')
    reservation, plan = financed(buyer, seller)

    from core.serializers import ReservationSerializer
    data = ReservationSerializer(reservation).data['installment']
    assert data['months'] == 10
    assert data['monthly_amount'] == '110000.00'
    assert data['card_last4'] == GOOD_CARD[-4:]


def test_a_wallet_reservation_has_no_installment_block():
    buyer, seller = make_user('ins_ser2_buyer'), make_user('ins_ser2_seller')
    reservation = make_reservation(buyer, seller)

    from core.serializers import ReservationSerializer
    assert ReservationSerializer(reservation).data['installment'] is None


# ---------------------------------------------------------------------------
# الـ webhook
# ---------------------------------------------------------------------------

def test_the_webhook_reads_only_the_reference(client):
    """يُقيَّد ما يقوله النداء الموثّق للمموّل، لا ما يقوله جسم الـ webhook —
    وإلّا استطاع من يجد الرابط أن يُضخّم المحصَّل، وهو الرقم الذي يُحسب
    منه ما يُعاد للمشتري."""
    buyer, seller = make_user('ins_wh_buyer'), make_user('ins_wh_seller')
    reservation, plan = financed(buyer, seller)
    InstallmentPlan.objects.filter(pk=plan.pk).update(
        applied_at=timezone.now() - timedelta(days=60))

    response = client.post('/api/installments/webhook/qicard/',
                           {'order_id': plan.reference,
                            'collected_amount': '999999999'}, format='json')
    assert response.status_code == 200

    plan.refresh_from_db()
    assert plan.collected_amount == Decimal('220000.00')


def test_an_unknown_reference_is_acknowledged_and_ignored(client):
    response = client.post('/api/installments/webhook/qicard/',
                           {'order_id': 'INS-nope'}, format='json')
    assert response.status_code == 200
    assert InstallmentPlan.objects.count() == 0


def test_a_bad_webhook_secret_changes_nothing(client, settings):
    settings.INSTALLMENT_PROVIDERS = {
        'qicard': dict(PROVIDER_SETTINGS['qicard'], WEBHOOK_SECRET='s3cret')}
    reset_cache()
    buyer, seller = make_user('ins_ws_buyer'), make_user('ins_ws_seller')
    reservation, plan = financed(buyer, seller)
    InstallmentPlan.objects.filter(pk=plan.pk).update(
        applied_at=timezone.now() - timedelta(days=60))

    client.post('/api/installments/webhook/qicard/?secret=wrong',
                {'order_id': plan.reference}, format='json')
    plan.refresh_from_db()
    assert plan.collected_amount == Decimal('0.00')
