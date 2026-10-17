"""بريد المنصّة مُطفأ — ‎settings.EMAIL_NOTIFICATIONS_ENABLED‎.

الراية مُطفأة افتراضياً حتى يُضبط مُرسِل حقيقي. وما يُحرَس هنا شيئان:

١. **أن البريد لا يخرج.** كل بريد في المنصّة يمرّ بـ
   ‎NotificationService.send_email‎، فالاختبار يراقب ناقل Django نفسه
   (‎mail.outbox‎) لا الدالّة — فلو أُضيف يوماً مسارٌ يستدعي ‎send_mail‎
   مباشرةً متجاوزاً الخدمة، سقط الاختبار.

٢. **أن الإشعار الفوري ما زال يخرج.** هذا هو الفرق الذي يُفقد بسهولة:
   المطلوب إطفاء البريد لا إسكات المنصّة. حجزٌ لا يُبلّغ صاحبه بشيء عطبٌ
   لا إعداد.
"""
from unittest.mock import patch

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import User
from core.notifications import NotificationService

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return User.objects.create_user(
        username='mushtari', password='Passw0rd!x',
        email='mushtari@example.com', phone='07701234567')


class TestNothingLeaves:
    def test_send_email_returns_false_and_sends_nothing(self, settings):
        settings.EMAIL_NOTIFICATIONS_ENABLED = False
        sent = NotificationService.send_email(
            'ahmad@example.com', 'موضوع', 'نصّ')
        assert sent is False
        assert mail.outbox == []

    def test_an_html_email_is_stopped_too(self, settings):
        # المسار الثاني داخل send_email (EmailMultiAlternatives) — الراية
        # تُفحص قبل الانقسام، فلا يُفلت أحدهما.
        settings.EMAIL_NOTIFICATIONS_ENABLED = False
        assert NotificationService.send_email(
            'ahmad@example.com', 'موضوع', 'نصّ',
            html_message='<p>نصّ</p>') is False
        assert mail.outbox == []

    def test_a_template_email_is_stopped(self, settings):
        settings.EMAIL_NOTIFICATIONS_ENABLED = False
        NotificationService.send_template_email(
            'ahmad@example.com', 'anything', {}, 'موضوع')
        assert mail.outbox == []

    def test_turning_it_on_sends_again(self, settings):
        # الراية تُطفئ ولا تحذف: يوم يُضبط المُرسِل يعود كل شيء بسطر في .env.
        settings.EMAIL_NOTIFICATIONS_ENABLED = True
        assert NotificationService.send_email(
            'ahmad@example.com', 'موضوع', 'نصّ') is True
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ['ahmad@example.com']

    def test_it_is_off_by_default(self):
        # لا راية في الاختبار = الإعداد الحقيقي. إشعالها يحتاج .env، لا نسياناً.
        from django.conf import settings as real
        assert real.EMAIL_NOTIFICATIONS_ENABLED is False


class TestThePushStillGoes:
    """المطلوب إطفاء البريد لا إسكات المنصّة."""

    def test_notify_push_writes_a_row_and_pushes(self, settings, user):
        from core.models import Notification
        from core.notifications import notify_push

        settings.EMAIL_NOTIFICATIONS_ENABLED = False
        # notify_push لا يدفع إلا بـFIREBASE_AVAILABLE ورمز جهاز — كلاهما
        # مُهيَّأ هنا، وإلّا لمرّ الاختبار بلا أن يُثبت شيئاً.
        user.fcm_token = 'fake-device-token'
        user.save(update_fields=['fcm_token'])
        with patch('core.notifications.FIREBASE_AVAILABLE', True),                 patch('core.notifications.send_push_notification') as push:
            notify_push(user, 'عنوان', 'نصّ')

        # الإشعار محفوظ ومُرسَل، والبريد صامت — هذا هو المطلوب بالضبط.
        assert Notification.objects.filter(user=user).exists()
        push.assert_called_once()
        assert mail.outbox == []


class TestTheEndpointsThatPromisedMail:
    """نقطتان كانتا تعدان برسالة. الوعد الكاذب أسوأ من الرفض الصريح."""

    def test_password_reset_by_email_says_it_is_unavailable(self, settings, user):
        settings.EMAIL_NOTIFICATIONS_ENABLED = False
        response = APIClient().post(
            reverse('reset-password'), {'email': user.email}, format='json')
        # كان يردّ 200 و«ستصلك رسالة» — فينتظر المستخدم بريداً لا يأتي.
        assert response.status_code == 503
        assert response.data['code'] == 'email_disabled'
        assert 'هاتفك' in response.data['message']
        assert mail.outbox == []

    def test_it_answers_the_same_for_an_unknown_email(self, settings):
        # الجواب عن قدرة المنصّة لا عن وجود الحساب، فلا يُستدلّ منه على
        # أي العناوين مسجّلة.
        settings.EMAIL_NOTIFICATIONS_ENABLED = False
        response = APIClient().post(
            reverse('reset-password'), {'email': 'nobody@example.com'},
            format='json')
        assert response.status_code == 503
        assert response.data['code'] == 'email_disabled'

    def test_resend_verification_is_503_not_try_again_later(
            self, settings, user):
        settings.EMAIL_NOTIFICATIONS_ENABLED = False
        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(reverse('resend-verification'))
        # 502 «حاول لاحقاً» كان وعداً كاذباً: لن يعمل لاحقاً حتى يُضبط المُرسِل.
        assert response.status_code == 503
        assert response.data['code'] == 'email_disabled'

    def test_registering_with_an_email_still_succeeds(self, settings):
        # رمز التفعيل لا يُرسَل، والتسجيل يجب أن يمضي: البريد اختياري
        # والهاتف هو الهوية.
        settings.EMAIL_NOTIFICATIONS_ENABLED = False
        response = APIClient().post(reverse('register'), {
            'username': 'jadeed',
            'email': 'jadeed@example.com',
            'phone': '07801234567',
            'password': 'Passw0rd!x',
            'password2': 'Passw0rd!x',
            'first_name': 'أحمد',
            'last_name': 'علي',
        }, format='json')
        assert response.status_code in (200, 201), response.data
        assert User.objects.filter(email='jadeed@example.com').exists()
        assert mail.outbox == []
