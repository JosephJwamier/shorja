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
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .auth_tokens import revoke_refresh_tokens
from .password_rules import password_error
from .phone_utils import to_e164
from .services.wevlix import (
    WevlixError, WevlixNotConfigured, send_otp, verify_otp,
)

logger = logging.getLogger(__name__)
User = None  # يُحلّ عند الاستعمال عبر get_user_model


def _cache_key(phone_e164):
    return f'otp:{phone_e164}'


def _failures_key(phone_e164):
    return f'otp-fail:{phone_e164}'


# محاولات خاطئة يُسمح بها على رمز واحد قبل إبطاله. الخنق وحده
# (otp_verify) مربوط بالـ IP، ومهاجم بعناوين كثيرة كان يجرّب الرمز نفسه
# بلا حدّ: الخطأ لم يكن يُبطل شيئاً. خمس محاولات تكفي من أخطأ الكتابة،
# وتجعل فرصة التخمين 5 من مليون لكل رمز مهما كثرت العناوين.
OTP_MAX_FAILURES = 5


def _service_unavailable():
    return Response(
        {'success': False, 'code': 'otp_unavailable',
         'message': 'خدمة التحقّق غير متاحة حالياً'},
        status=status.HTTP_503_SERVICE_UNAVAILABLE)


def _daily_key():
    # تاريخ بغداد في المفتاح: العدّاد يبدأ من الصفر عند منتصف ليلنا، ويُمحى
    # وحده بعد يومين.
    return f'otp-sent:{timezone.localdate().isoformat()}'


def _take_daily_slot():
    """يحجز رسالة من سقف اليوم ويعيد مفتاح عدّاده، أو `None` إن امتلأ.

    الحجز قبل الإرسال لا بعده: طلبان متزامنان عند آخر مكان كانا سيمرّان
    معاً لو فُحص العدد ثم أُرسل. add ثم incr ذرّيّتان على Redis.
    """
    key = _daily_key()
    cache.add(key, 0, timeout=2 * 24 * 3600)
    sent = cache.incr(key)
    cap = settings.OTP_DAILY_SEND_CAP
    if sent == max(1, cap * 8 // 10):
        logger.error('OTP sends reached %d of today\'s cap of %d', sent, cap)
    if sent > cap:
        if sent == cap + 1:
            logger.error('OTP daily cap of %d reached; refusing sends until midnight', cap)
        return None
    return key


def _give_back_daily_slot(key):
    """رسالة حُجزت ولم تُرسل (فشل Wevlix) لا تُحسب على السقف. المفتاح من
    الحجز نفسه: إرجاعٌ بعد منتصف الليل لا يُنقص عدّاد اليوم التالي."""
    try:
        cache.decr(key)
    except ValueError:  # المفتاح انتهى بين الحجز والإرجاع
        pass


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

        # يبدو للمستخدم كتعطّل عابر لا كسقف: لا شيء يخبر المهاجم أنه وصل
        # إلى الحدّ فيعرف متى يعود.
        slot = _take_daily_slot()
        if slot is None:
            return _service_unavailable()

        try:
            message_id = send_otp(canonical)
        except WevlixNotConfigured:
            _give_back_daily_slot(slot)
            logger.error('OTP requested but Wevlix is not configured')
            return _service_unavailable()
        except WevlixError as e:
            _give_back_daily_slot(slot)
            return Response({'success': False, 'message': str(e)},
                            status=status.HTTP_502_BAD_GATEWAY)

        cache.set(key, message_id, settings.OTP_TTL_SECONDS)
        cache.delete(_failures_key(canonical))  # رمز جديد، عدّاد جديد
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
        failures_key = _failures_key(canonical)
        # add ثم incr: ذرّيّتان على Redis، فطلبان متزامنان لا يضيّعان عدّة.
        cache.add(failures_key, 0, settings.OTP_TTL_SECONDS)
        failures = cache.incr(failures_key)
        if failures >= OTP_MAX_FAILURES:
            cache.delete_many([_cache_key(canonical), failures_key])
            logger.warning('OTP for %s burned after %d wrong attempts',
                           canonical[-4:], failures)
            return False, Response(
                {'success': False, 'code': 'otp_too_many_attempts',
                 'message': 'محاولات خاطئة كثيرة — اطلب رمزاً جديداً'},
                status=status.HTTP_400_BAD_REQUEST)
        return False, Response(
            {'success': False, 'message': 'الرمز غير صحيح'},
            status=status.HTTP_400_BAD_REQUEST)

    # يُستهلك مرة واحدة: رمز صالح بعد استعماله يسمح بإعادة تشغيل الطلب
    # نفسه (توثيق رقم، أو إعادة تعيين كلمة مرور) مرّات.
    cache.delete_many([_cache_key(canonical), _failures_key(canonical)])
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
        with transaction.atomic():
            user.save(update_fields=['password', 'phone_verified'])
            # من يسترجع حسابه قد يكون فقده لغيره: كل جهاز مسجّل يخرج.
            revoke_refresh_tokens(user)
        logger.info('Password reset by WhatsApp code for user %s', user.id)
        return Response(self.GENERIC_OK)
