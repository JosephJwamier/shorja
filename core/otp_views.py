"""نقاط رمز التحقّق عبر Wevlix (واتساب).

تحلّ محلّ المسار القديم الذي كانت فيه Firebase ترسل الرمز من التطبيق
وتُرجع رمز هوية يتحقّق منه الخادم. الآن الخادم هو من يطلب الإرسال، فيملك
الخنق والتسجيل وإيقاف النزيف — وهي أمور لم يكن يملكها حين كان الجهاز
يُرسل مباشرة.

الرمز نفسه لا يمرّ بنا: Wevlix تولّده وتخزّن تجزئته وتقارنه. ما نحفظه هو
messageId مربوطاً بالرقم، في الذاكرة المؤقتة لا في قاعدة البيانات — فهو
يعيش دقائق ولا معنى لبقائه.
"""
import logging

from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .password_rules import password_error
from .phone_utils import to_e164
from .services.wevlix import (
    WevlixError, WevlixNotConfigured, send_otp, verify_otp,
)

logger = logging.getLogger(__name__)
User = None  # يُحلّ عند الاستعمال عبر get_user_model


def _cache_key(phone_e164):
    return f'otp:{phone_e164}'


def _service_unavailable():
    return Response(
        {'success': False, 'code': 'otp_unavailable',
         'message': 'خدمة التحقّق غير متاحة حالياً'},
        status=status.HTTP_503_SERVICE_UNAVAILABLE)


class SendPhoneCodeView(APIView):
    """يرسل رمزاً إلى رقم عبر واتساب.

    مفتوح بلا مصادقة لأن الاسترجاع يسبق الدخول — ولهذا هو **أغلى نقطة
    في النظام**: كل نداء ناجح يكلّف رسالة. الخنق هنا ليس حماية من إساءة
    فحسب، بل من فاتورة.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_send'

    def post(self, request):
        canonical = to_e164(request.data.get('phone'))
        if not canonical:
            return Response(
                {'success': False,
                 'message': 'رقم هاتف عراقي غير صالح — يجب أن يبدأ بـ 075 أو 077 أو 078 أو 079'},
                status=status.HTTP_400_BAD_REQUEST)

        key = _cache_key(canonical)

        # رمز سارٍ بالفعل: لا نرسل ثانية. ضغطة مزدوجة أو إعادة محاولة
        # سريعة كانت ستكلّف رسالتين مقابل لا شيء — والرمز الأول ما زال
        # في يد المستخدم.
        existing = cache.get(key)
        if existing:
            return Response({
                'success': True,
                'message': 'رمز التحقّق مُرسَل بالفعل — تحقّق من واتساب',
                'already_sent': True,
            })

        try:
            message_id = send_otp(canonical)
        except WevlixNotConfigured:
            logger.error('OTP requested but Wevlix is not configured')
            return _service_unavailable()
        except WevlixError as e:
            return Response({'success': False, 'message': str(e)},
                            status=status.HTTP_502_BAD_GATEWAY)

        cache.set(key, message_id, settings.OTP_TTL_SECONDS)
        logger.info('OTP sent to %s (message %s)', canonical[-4:], message_id)
        return Response({
            'success': True,
            'message': 'أُرسل رمز التحقّق عبر واتساب',
            'already_sent': False,
        })


def _check_code(canonical, code):
    """(ok, error_response). يستهلك الرمز عند النجاح."""
    message_id = cache.get(_cache_key(canonical))
    if not message_id:
        return False, Response(
            {'success': False, 'message': 'انتهت صلاحية الرمز — اطلب رمزاً جديداً'},
            status=status.HTTP_400_BAD_REQUEST)

    try:
        ok = verify_otp(message_id, code)
    except WevlixNotConfigured:
        return False, _service_unavailable()
    except WevlixError as e:
        return False, Response({'success': False, 'message': str(e)},
                               status=status.HTTP_502_BAD_GATEWAY)

    if not ok:
        return False, Response(
            {'success': False, 'message': 'الرمز غير صحيح'},
            status=status.HTTP_400_BAD_REQUEST)

    # يُستهلك مرة واحدة: رمز صالح بعد استعماله يسمح بإعادة تشغيل الطلب
    # نفسه (توثيق رقم، أو إعادة تعيين كلمة مرور) مرّات.
    cache.delete(_cache_key(canonical))
    return True, None


class VerifyPhoneCodeView(APIView):
    """يوثّق رقم المستخدم الحالي بعد إدخال الرمز."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_verify'

    def post(self, request):
        user = request.user
        if user.phone_verified:
            return Response({'success': True, 'message': 'الرقم موثّق بالفعل'})

        canonical = to_e164(request.data.get('phone') or user.phone)
        if not canonical:
            return Response({'success': False, 'message': 'رقم هاتف غير صالح'},
                            status=status.HTTP_400_BAD_REQUEST)

        # الرمز يثبت ملكية رقم؛ وهذا يثبت أنه رقم **هذا الحساب**.
        # بدونه يستطيع أحدهم توثيق حسابه برقم يملكه فعلاً لكنه ليس
        # الرقم المسجّل عليه.
        if to_e164(user.phone) != canonical:
            return Response(
                {'success': False, 'message': 'الرقم لا يطابق رقم حسابك'},
                status=status.HTTP_400_BAD_REQUEST)

        ok, error = _check_code(canonical, str(request.data.get('code') or ''))
        if not ok:
            return error

        user.phone_verified = True
        user.save(update_fields=['phone_verified'])
        return Response({'success': True, 'message': 'تم توثيق رقم هاتفك'})


class ResetPasswordByCodeView(APIView):
    """يعيد تعيين كلمة المرور بعد إثبات ملكية الرقم.

    يردّ الجواب نفسه سواء كان الرقم مسجّلاً أم لا، فلا يصلح لمعرفة أي
    الأرقام تملك حسابات.
    """
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_verify'

    GENERIC_OK = {'success': True,
                  'message': 'إن كان الرقم مسجّلاً فقد تغيّرت كلمة المرور'}

    def post(self, request):
        from django.contrib.auth import get_user_model

        canonical = to_e164(request.data.get('phone'))
        new_password = request.data.get('new_password') or ''
        if not canonical:
            return Response({'success': False, 'message': 'رقم هاتف غير صالح'},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(new_password) < 8:
            return Response(
                {'success': False, 'message': 'كلمة المرور يجب أن تكون 8 أحرف على الأقل'},
                status=status.HTTP_400_BAD_REQUEST)

        ok, error = _check_code(canonical, str(request.data.get('code') or ''))
        if not ok:
            return error

        user = get_user_model().objects.filter(phone=canonical).first()
        if user is None:
            return Response(self.GENERIC_OK)

        # بعد إيجاد المستخدم كي يعمل مُدقِّق التشابه عليه هو. والرمز
        # استُهلك قبل هذا السطر، فرفضُ كلمة ضعيفة يعني طلب رمز جديد —
        # وهو الثمن الصحيح: الفحص قبل الاستهلاك كان سيكشف أي الأرقام
        # لها حسابات.
        error = password_error(new_password, user=user)
        if error:
            return Response({'success': False, 'message': error},
                            status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        # الرمز وصل إلى الرقم، فملكيته مثبتة — وتسجيل ذلك يوفّر على
        # المستخدم توثيقاً ثانياً فور استرجاعه.
        user.phone_verified = True
        user.save(update_fields=['password', 'phone_verified'])
        logger.info('Password reset by WhatsApp code for user %s', user.id)
        return Response(self.GENERIC_OK)
