"""نشر منتج بلا عربون.

«بدون عربون» في التطبيق تُرسل deposit_percentage = 0، وهي الحالة
الافتراضية الآن — أي المسار الذي يسلكه أغلب البائعين. يُختبر هنا لأن
الصفر قيمة حدّية تسقط بسهولة من تحقّق مكتوب بـ `if not value`.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from core.models import Category, FixedPriceListing, Item, Location

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def seller():
    user = User.objects.create_user(
        username='seller', password='x', phone='07701234567')
    Location.objects.create(
        user=user, name='المخزن', address_line1='شارع', city='بغداد',
        state='بغداد', country='العراق', contact_person='س',
        contact_phone='07701234567', is_primary=True)
    return user


@pytest.fixture
def client(seller):
    api = APIClient()
    api.force_authenticate(user=seller)
    return api


@pytest.fixture
def draft(seller):
    category = Category.objects.create(name='Phones', name_ar='هواتف')
    return Item.objects.create(
        owner=seller, title='Galaxy', name_ar='غالاكسي', description='d',
        category=category, condition='used', status='draft', quantity=1)


def post_listing(client, item, percentage):
    return client.post(
        f'/api/items/{item.id}/create_fixed_listing/',
        {'price': '250000', 'deposit_percentage': str(percentage)},
        format='multipart')


def test_zero_deposit_is_accepted(client, draft):
    r = post_listing(client, draft, 0)
    assert r.status_code in (200, 201), r.data


def test_zero_deposit_stores_a_zero_amount(client, draft):
    post_listing(client, draft, 0)
    listing = FixedPriceListing.objects.get()
    assert listing.deposit_percentage == 0
    assert listing.deposit_amount == 0


def test_a_real_deposit_is_computed_from_the_price(client, draft):
    post_listing(client, draft, 10)
    listing = FixedPriceListing.objects.get()
    assert listing.deposit_amount == 25000  # 10% من 250,000


def test_a_negative_deposit_is_refused(client, draft):
    assert post_listing(client, draft, -5).status_code == 400


def test_over_a_hundred_percent_is_refused(client, draft):
    assert post_listing(client, draft, 150).status_code == 400
