"""‎AUTH_PASSWORD_VALIDATORS‎ تُنفَّذ فعلاً — على المسارات الأربعة.

المُدقِّقات مُعلَنة في `settings.py` منذ البداية و`validate_password` لم تكن
تُنادى في أي منها: التسجيل، وتغيير كلمة المرور، والاسترجاع بالبريد،
والاسترجاع بواتساب. فكل ما فُرض هو `min_length=8` على حقل المُسلسِل — أي
أن `12345678` و`password` كانتا مقبولتين على سوق فيه محافظ.

إعدادٌ مُعلَن لا يُنفَّذ أسوأ من إعدادٍ غائب: الغائب يُلاحَظ، والمُعلَن
يُقرأ في المراجعة فيُظنّ مُطبَّقاً. فالاختبار هنا على **النقاط** لا على
`password_error` وحدها — فحصُ الدالّة يُثبت أنها تعمل، لا أن أحداً يناديها.
"""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import User
from core.password_rules import password_error
from core.tests.phones import next_phone

pytestmark = pytest.mark.django_db

STRONG = 'Tajir!Shorja#2026'


def register(client, password, **overrides):
    payload = {
        'first_name': 'أحمد', 'last_name': 'علي',
        'phone': next_phone(), 'email': '',
        'password': password, 'password2': password,
    }
    payload.update(overrides)
    return client.post(reverse('register'), payload, format='json')


@pytest.fixture
def client():
    return APIClient()


class TestRegistration:
    @pytest.mark.parametrize('password, because', [
        ('12345678', 'أرقام فقط'),
        ('password', 'شائعة'),
        ('password123', 'شائعة'),
        ('qwerty123', 'شائعة'),
    ])
    def test_a_weak_password_is_refused(self, client, password, because):
        response = register(client, password)
        assert response.status_code == 400, because
        assert 'password' in response.data.get('errors', response.data)

    def test_the_reason_is_in_arabic(self, client):
        # رسالة Django إنجليزية، ومستخدمنا عربي — و«مرفوضة» بلا سبب تجعله
        # يجرّب كلمةً أخرى بنفس العيب.
        #
        # ورقمٌ غير شائع عن قصد: `12345678` في قائمة Django الشائعة،
        # فيردّ عليها CommonPasswordValidator قبل أن يراها مُدقِّق الأرقام.
        response = register(client, '47382910')
        body = str(response.data)
        assert 'أرقام' in body, body

    def test_a_password_built_on_the_email_is_refused(self, client):
        # UserAttributeSimilarityValidator يقارن بأجزاء الحقل لا بكلّه:
        # `kareem2026@example.com` تُقسَّم، فيُطابق الجزء الأول تماماً.
        response = register(
            client, 'kareem2026', email='kareem2026@example.com')
        assert response.status_code == 400, response.data

    def test_a_strong_password_is_accepted(self, client):
        response = register(client, STRONG)
        assert response.status_code in (200, 201), response.data

    def test_short_is_still_refused(self, client):
        # السلوك القديم الوحيد الذي كان يعمل — لا يتغيّر.
        assert register(client, 'Ab3!x').status_code == 400


class TestChangingPassword:
    def test_a_weak_new_password_is_refused(self, client):
        user = User.objects.create_user(
            username='tajir', password=STRONG, phone=next_phone())
        client.force_authenticate(user=user)
        response = client.post(reverse('change-password'), {
            'old_password': STRONG, 'new_password': '12345678',
            'new_password2': '12345678',
        }, format='json')
        assert response.status_code == 400
        user.refresh_from_db()
        assert user.check_password(STRONG), 'كلمة المرور القديمة يجب أن تبقى'

    def test_a_strong_new_password_is_accepted(self, client):
        user = User.objects.create_user(
            username='tajir2', password=STRONG, phone=next_phone())
        client.force_authenticate(user=user)
        response = client.post(reverse('change-password'), {
            'old_password': STRONG, 'new_password': 'Suq!Baghdad#99',
            'new_password2': 'Suq!Baghdad#99',
        }, format='json')
        assert response.status_code == 200, response.data
        user.refresh_from_db()
        assert user.check_password('Suq!Baghdad#99')


class TestResetByWhatsappCode:
    def _send_and_reset(self, client, user, new_password):
        from unittest.mock import patch
        phone = user.phone
        with patch('core.otp_views.send_otp', return_value='msg-1'):
            client.post(reverse('phone-send-code'), {'phone': phone},
                        format='json')
        with patch('core.otp_views.verify_otp', return_value=True):
            return client.post(reverse('reset-password-code'), {
                'phone': phone, 'code': '123456',
                'new_password': new_password,
            }, format='json')

    def test_a_weak_password_is_refused(self, client):
        user = User.objects.create_user(
            username='tajir3', password=STRONG, phone=next_phone())
        response = self._send_and_reset(client, user, '12345678')
        assert response.status_code == 400
        user.refresh_from_db()
        assert user.check_password(STRONG)

    def test_a_strong_password_goes_through(self, client):
        user = User.objects.create_user(
            username='tajir4', password=STRONG, phone=next_phone())
        response = self._send_and_reset(client, user, 'Suq!Baghdad#99')
        assert response.status_code == 200, response.data
        user.refresh_from_db()
        assert user.check_password('Suq!Baghdad#99')


class TestTheHelperItself:
    def test_it_returns_none_for_a_good_password(self):
        assert password_error(STRONG) is None

    def test_every_rejection_carries_a_reason(self):
        for password in ('short', '12345678', 'password', 'abcdefgh'):
            message = password_error(password)
            if message is not None:
                assert message.strip(), password

    def test_similarity_needs_the_user_to_work(self):
        # تطابق تامّ: العتبة 0.7، و`kareemkareem` مقابل `kareem` تبلغ
        # 0.67 فتمرّ — فالمثال هنا ما يتطابق لا ما يشبه.
        user = User(username='kareemjwamier')
        assert password_error('kareemjwamier', user=user) is not None
        # بلا مستخدم لا شيء تُقارَن به — وهذا سبب تمريره في كل نداء.
        assert password_error('kareemjwamier') is None
