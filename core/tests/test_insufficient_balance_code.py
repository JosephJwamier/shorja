"""رمز الرصيد غير الكافي.

التطبيق يحوّل المشتري إلى شاشة شحن المحفظة حين يقرأ هذا الرمز. لو اعتمد
على نصّ الرسالة لانكسر عند أول إعادة صياغة أو ترجمة — والانكسار صامت:
يظهر خطأ عامّ بدل طريق الخروج، ولا يفشل شيء ظاهر.
"""
import pytest
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient

from core.models import (
    Category, FixedPriceListing, Item, Location, Reservation, Wallet,
)

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
def reservation_needing_a_deposit():
    seller = make_user('seller', '07701234567')
    buyer = make_user('buyer', '07801234567')

    category = Category.objects.create(name='Phones', name_ar='هواتف')
    item = Item.objects.create(
        owner=seller, title='Galaxy', name_ar='غالاكسي', description='d',
        category=category, condition='used', status='active', quantity=1)
    FixedPriceListing.objects.create(
        item=item, price=Decimal('250000'), deposit_percentage=Decimal('10'))

    # محفظة فارغة: العربون 25,000 والرصيد صفر.
    Wallet.objects.get_or_create(user=buyer)

    # لا حقل seller على Reservation — البائع يُشتقّ من item.owner.
    return Reservation.objects.create(
        item=item, buyer=buyer,
        quantity=1, total_price=Decimal('250000'),
        deposit_amount=Decimal('25000'),
        delivery_location=buyer.locations.first(),
        status='pending', expires_at=timezone.now() + timedelta(days=1))


def test_an_empty_wallet_returns_the_insufficient_balance_code(
        reservation_needing_a_deposit):
    client = APIClient()
    client.force_authenticate(user=reservation_needing_a_deposit.buyer)

    r = client.post(
        f'/api/reservations/{reservation_needing_a_deposit.id}/complete/',
        {'payment_type': 'deposit'}, format='json')

    assert r.status_code == 400, r.data
    # الرمز لا الرسالة: هذا ما يفتح شاشة الشحن في التطبيق.
    assert r.data.get('code') == 'insufficient_balance', r.data
