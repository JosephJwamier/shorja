"""تبسيط نموذج إضافة الموقع.

كان النموذج يطلب ثمانية حقول إجبارية، اثنان منها بلا معنى في العراق
(الرمز البريدي: لا نظام بريدي عاملاً) أو معروفان سلفاً (الدولة، واسم
المستخدم ورقمه). ما يُختبر هنا هو أن الخادم لم يعد يشترط ما حُذف.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from core.models import Location

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def seller():
    return User.objects.create_user(
        username='seller', password='x', phone='07701234567',
        first_name='عبد', last_name='الرحمن')


@pytest.fixture
def client(seller):
    api = APIClient()
    api.force_authenticate(user=seller)
    return api


MINIMAL = {
    'location_type': 'user_address',
    'name': 'موقعي في بغداد',
    'address_line1': 'قرب جامع النداء',
    'city': 'بغداد',
    'state': 'بغداد',
    'contact_person': 'عبد الرحمن',
    'contact_phone': '07701234567',
}


def test_a_location_saves_without_country_or_postal_code(client):
    r = client.post('/api/locations/', MINIMAL, format='json')
    assert r.status_code == 201, r.data


def test_country_defaults_to_iraq(client):
    client.post('/api/locations/', MINIMAL, format='json')
    assert Location.objects.get().country == 'العراق'


def test_postal_code_stays_empty_rather_than_demanded(client):
    client.post('/api/locations/', MINIMAL, format='json')
    assert Location.objects.get().postal_code == ''


def test_the_governorate_is_still_required(client):
    # 35 موضعاً في الخادم يقرأ state — أجرة التوصيل تُحسب منها.
    # التبسيط لا يمسّ الحقل الوحيد الذي يحمل منطقاً.
    body = {**MINIMAL}
    del body['state']
    r = client.post('/api/locations/', body, format='json')
    assert r.status_code == 400
    # الواجهة تلفّ أخطاء الحقول داخل 'errors' بدل جذر الاستجابة.
    assert 'state' in r.data['errors']


def test_coordinates_are_accepted_when_the_pin_is_set(client):
    r = client.post('/api/locations/',
                    {**MINIMAL, 'latitude': '33.315200', 'longitude': '44.366100'},
                    format='json')
    assert r.status_code == 201, r.data
    saved = Location.objects.get()
    assert float(saved.latitude) == pytest.approx(33.3152)
