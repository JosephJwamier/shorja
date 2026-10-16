"""رمز التحقّق عبر Wevlix (واتساب).

Wevlix مُحاكاة عند حدود core.services.wevlix — لا شبكة ولا مفتاح ولا
رسالة تُرسَل أثناء الاختبار.

أكثر ما يُختبر هنا هو ما **لا** يُرسَل: كل نداء ناجح لنقطة الإرسال يكلّف
رسالة حقيقية، فالحماية من الإرسال المكرّر ليست تحسيناً بل ضبطاً للفاتورة.
"""
import pytest
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from core.services.wevlix import WevlixError, WevlixNotConfigured

User = get_user_model()
pytestmark = pytest.mark.django_db

PHONE = '07701234567'
E164 = '+9647701234567'
MESSAGE_ID = 'msg-abc-123'


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def user():
    return User.objects.create_user(
        username='tajir', password='Passw0rd!x', phone=PHONE)


def send(client, phone=PHONE, **patches):
    with patch('core.otp_views.send_otp', **patches) as m:
        m.return_value = patches.get('return_value', MESSAGE_ID)
        return client.post(reverse('phone-send-code'), {'phone': phone},
                           format='json'), m


class TestSending:
    def test_a_valid_number_gets_a_code(self, client):
        response, sent = send(client)
        assert response.status_code == 200, response.data
        sent.assert_called_once_with(E164)

    def test_any_spelling_reaches_the_same_canonical_number(self, client):
        _, sent = send(client, phone='+964 770 123 4567')
        sent.assert_called_once_with(E164)

    def test_an_invalid_number_never_reaches_wevlix(self, client):
        # الرفض قبل النداء: رقم فاسد كان سيكلّف رسالة ضائعة.
        response, sent = send(client, phone='07401234567')
        assert response.status_code == 400
        sent.assert_not_called()

    def test_a_second_request_while_one_is_live_sends_nothing(self, client):
        send(client)
        response, sent = send(client)
        assert response.status_code == 200
        assert response.data['already_sent'] is True
        # ضغطة مزدوجة كانت ستكلّف رسالتين والرمز الأول بيد المستخدم.
        sent.assert_not_called()

    def test_a_missing_api_key_is_503_not_a_crash(self, client):
        with patch('core.otp_views.send_otp',
                   side_effect=WevlixNotConfigured('no key')):
            r = client.post(reverse('phone-send-code'), {'phone': PHONE},
                            format='json')
        assert r.status_code == 503
        assert r.data['code'] == 'otp_unavailable'

    def test_an_upstream_outage_is_502_and_stores_nothing(self, client):
        with patch('core.otp_views.send_otp', side_effect=WevlixError('down')):
            r = client.post(reverse('phone-send-code'), {'phone': PHONE},
                            format='json')
        assert r.status_code == 502
        # لا messageId محفوظ، فالمحاولة التالية ترسل فعلاً.
        assert cache.get(f'otp:{E164}') is None


class TestVerifyingAPhone:
    def test_the_right_code_verifies_the_account(self, client, user):
        send(client)
        client.force_authenticate(user=user)
        with patch('core.otp_views.verify_otp', return_value=True):
            r = client.post(reverse('phone-verify-code'),
                            {'phone': PHONE, 'code': '123456'}, format='json')
        assert r.status_code == 200, r.data
        user.refresh_from_db()
        assert user.phone_verified is True

    def test_a_wrong_code_changes_nothing(self, client, user):
        send(client)
        client.force_authenticate(user=user)
        with patch('core.otp_views.verify_otp', return_value=False):
            r = client.post(reverse('phone-verify-code'),
                            {'phone': PHONE, 'code': '000000'}, format='json')
        assert r.status_code == 400
        user.refresh_from_db()
        assert user.phone_verified is False

    def test_a_code_cannot_be_used_twice(self, client, user):
        send(client)
        client.force_authenticate(user=user)
        with patch('core.otp_views.verify_otp', return_value=True):
            client.post(reverse('phone-verify-code'),
                        {'phone': PHONE, 'code': '123456'}, format='json')
            user.phone_verified = False
            user.save(update_fields=['phone_verified'])
            r = client.post(reverse('phone-verify-code'),
                            {'phone': PHONE, 'code': '123456'}, format='json')
        # استُهلك عند أول نجاح، فلا يُعاد تشغيل الطلب به.
        assert r.status_code == 400

    def test_verifying_someone_elses_number_is_refused(self, client, user):
        other = '07801234567'
        send(client, phone=other)
        client.force_authenticate(user=user)
        with patch('core.otp_views.verify_otp', return_value=True):
            r = client.post(reverse('phone-verify-code'),
                            {'phone': other, 'code': '123456'}, format='json')
        # يملك الرقم الآخر فعلاً، لكنه ليس رقم حسابه.
        assert r.status_code == 400
        user.refresh_from_db()
        assert user.phone_verified is False

    def test_an_expired_code_asks_for_a_new_one(self, client, user):
        client.force_authenticate(user=user)
        with patch('core.otp_views.verify_otp', return_value=True) as v:
            r = client.post(reverse('phone-verify-code'),
                            {'phone': PHONE, 'code': '123456'}, format='json')
        assert r.status_code == 400
        # لا messageId محفوظ، فلا معنى لسؤال Wevlix أصلاً.
        v.assert_not_called()


class TestResettingAPassword:
    def test_the_password_changes_and_the_phone_becomes_verified(
            self, client, user):
        send(client)
        with patch('core.otp_views.verify_otp', return_value=True):
            r = client.post(reverse('reset-password-code'),
                            {'phone': PHONE, 'code': '123456',
                             'new_password': 'NewPassw0rd!'}, format='json')
        assert r.status_code == 200, r.data
        user.refresh_from_db()
        assert user.check_password('NewPassw0rd!')
        assert user.phone_verified is True

    def test_an_unregistered_number_answers_the_same(self, client):
        send(client, phone='07801234567')
        with patch('core.otp_views.verify_otp', return_value=True):
            r = client.post(reverse('reset-password-code'),
                            {'phone': '07801234567', 'code': '123456',
                             'new_password': 'NewPassw0rd!'}, format='json')
        # جواب واحد للحالتين، فلا يُستدلّ منه على أي الأرقام لها حسابات.
        assert r.status_code == 200
        assert r.data['message'] == \
            'إن كان الرقم مسجّلاً فقد تغيّرت كلمة المرور'

    def test_a_short_password_is_refused_before_the_code_is_spent(
            self, client, user):
        send(client)
        with patch('core.otp_views.verify_otp', return_value=True) as v:
            r = client.post(reverse('reset-password-code'),
                            {'phone': PHONE, 'code': '123456',
                             'new_password': 'short'}, format='json')
        assert r.status_code == 400
        v.assert_not_called()
        # الرمز ما زال صالحاً: الخطأ كان في كلمة المرور لا فيه.
        assert cache.get(f'otp:{E164}') == MESSAGE_ID
