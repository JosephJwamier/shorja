"""التسجيل بلا اسم مستخدم.

الاسم الأول والأخير يُعرّفان التاجر، والرقم يُدخله. اسم المستخدم كان حقلاً
ثالثاً يُجبره على ابتكار معرّف إنجليزي فريد ثم تذكّره — وهو عمود في جانغو
(USERNAME_FIELD) لا يمكن إسقاطه بلا كسر لوحة الإدارة، فيُولَّد صامتاً.
"""
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APIClient

User = get_user_model()
pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return APIClient()


def payload(**overrides):
    body = {
        'first_name': 'عبد', 'last_name': 'الرحمن',
        'phone': '07701234567',
        'password': 'Passw0rd!x', 'password2': 'Passw0rd!x',
    }
    body.update(overrides)
    return body


def test_signup_succeeds_with_no_username_field(client):
    r = client.post(reverse('register'), payload(), format='json')
    assert r.status_code == 201, r.data


def test_a_username_is_generated_from_the_phone(client):
    client.post(reverse('register'), payload(), format='json')
    user = User.objects.get(phone='+9647701234567')
    assert user.username == 'u9647701234567'


def test_two_spellings_of_one_phone_cannot_both_register(client):
    client.post(reverse('register'), payload(), format='json')
    r = client.post(reverse('register'),
                    payload(phone='+964 770 123 4567'), format='json')
    assert r.status_code == 400
    # التسجيل يُرجع أخطاء الحقول في الجذر؛ الحجوزات تلفّها في 'errors'.
    # الشكلان مختلفان في هذا الـ API، والاختبار يصف ما تفعله هذه النقطة.
    assert 'phone' in r.data


def test_the_generated_username_does_not_collide(client):
    # حساب قديم يحمل المعرّف المشتقّ مسبقاً — التوليد يلتفّ حوله بدل
    # أن يفشل التسجيل بخطأ تفرّد غامض.
    User.objects.create_user(username='u9647701234567', password='x')
    r = client.post(reverse('register'), payload(), format='json')
    assert r.status_code == 201, r.data
    assert User.objects.get(phone='+9647701234567').username == 'u9647701234567_2'


def test_login_by_phone_works_for_an_account_that_never_chose_a_username(client):
    client.post(reverse('register'), payload(), format='json')
    r = client.post(reverse('login'),
                    {'username': '07701234567', 'password': 'Passw0rd!x'},
                    format='json')
    assert r.status_code == 200, r.data
