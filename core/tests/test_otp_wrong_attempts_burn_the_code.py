"""خمس محاولات خاطئة تُبطل الرمز.

كان الخطأ لا يُبطل شيئاً، والخنق (otp_verify) مربوط بالـ IP فقط. فمهاجم
بعناوين كثيرة يعرف رقم الضحية كان يستطيع تجربة رمز إعادة تعيين كلمة المرور
بلا حدّ حتى يصيب، فيستولي على الحساب.

الآن يُعدّ الخطأ لكل رقم، وعند الخامس يُحذف الرمز: لا ينفع بعده حتى الرمز
الصحيح، ويلزم طلب رمز جديد (وهذا بدوره محدود بـ otp_send).
"""
import pytest
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from core.otp_views import OTP_MAX_FAILURES

User = get_user_model()
pytestmark = pytest.mark.django_db

PHONE = '07701234567'
E164 = '+9647701234567'
OLD_PASSWORD = 'Passw0rd!x'
NEW_PASSWORD = 'Brand-new-Pass9'


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def victim():
    return User.objects.create_user(username='victim', password=OLD_PASSWORD, phone=E164)


def request_code(client, message_id='msg-1'):
    with patch('core.otp_views.send_otp', return_value=message_id):
        return client.post(reverse('phone-send-code'), {'phone': PHONE}, format='json')


def try_reset(client, code_is_right):
    with patch('core.otp_views.verify_otp', return_value=code_is_right):
        return client.post(reverse('reset-password-code'), {
            'phone': PHONE, 'code': '123456', 'new_password': NEW_PASSWORD,
        }, format='json')


def test_the_right_code_is_useless_after_too_many_wrong_ones(victim):
    client = APIClient()
    request_code(client)

    for _ in range(OTP_MAX_FAILURES):
        try_reset(client, code_is_right=False)
    response = try_reset(client, code_is_right=True)

    assert response.status_code == 400
    victim.refresh_from_db()
    assert victim.check_password(OLD_PASSWORD)


def test_the_last_wrong_attempt_says_to_request_a_new_code(victim):
    client = APIClient()
    request_code(client)

    responses = [try_reset(client, code_is_right=False) for _ in range(OTP_MAX_FAILURES)]

    assert all(r.status_code == 400 for r in responses)
    assert responses[-1].data['code'] == 'otp_too_many_attempts'
    assert 'code' not in responses[0].data  # earlier ones are a plain "wrong code"


def test_a_typo_or_two_still_lets_the_owner_in(victim):
    client = APIClient()
    request_code(client)

    for _ in range(OTP_MAX_FAILURES - 1):
        try_reset(client, code_is_right=False)
    response = try_reset(client, code_is_right=True)

    assert response.status_code == 200, response.data
    victim.refresh_from_db()
    assert victim.check_password(NEW_PASSWORD)


def test_a_fresh_code_starts_a_fresh_count(victim):
    client = APIClient()
    request_code(client)
    for _ in range(OTP_MAX_FAILURES):
        try_reset(client, code_is_right=False)

    request_code(client, message_id='msg-2')
    for _ in range(OTP_MAX_FAILURES - 1):
        try_reset(client, code_is_right=False)
    response = try_reset(client, code_is_right=True)

    assert response.status_code == 200, response.data
