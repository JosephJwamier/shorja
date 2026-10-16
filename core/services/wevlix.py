"""عميل Wevlix لرسائل التحقّق (OTP) عبر واتساب.

لماذا انتقل التحقّق من العميل إلى الخادم: Firebase Phone Auth كانت تُرسل
الرسالة **من التطبيق مباشرة**، فلا يرى خادمنا الطلب ولا يستطيع خنقه — ومن
يطلب الرمز مئة مرة يكلّفنا مئة رسالة بلا أن نعلم. Wevlix تُستدعى بمفتاح
سرّي من الخادم، فكل رسالة تمرّ بنا: نحدّها، ونسجّلها، ونوقفها.

التدفّق مرحلتان:

    1. send(phone)   -> Wevlix تولّد الرمز وترسله، وتُرجع messageId
    2. verify(id, code) -> Wevlix تقارن الرمز بما خزّنته

الرمز لا يمرّ بنا في الاتجاهين: لا نولّده ولا نخزّنه ولا نقارنه. ما نحفظه
هو messageId وحده، مربوطاً بالرقم، إلى أن يصل الرمز من المستخدم.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15


class WevlixError(Exception):
    """فشل قابل للعرض للمستخدم."""


class WevlixNotConfigured(WevlixError):
    """لا مفتاح API.

    نوع مستقلّ لا رسالة: هذه ليست عطباً بل إعداداً ناقصاً، وتستحقّ من
    الواجهة 503 ورسالة «الخدمة غير متاحة» لا 400 توحي بخطأ المستخدم.
    """


def _api_key():
    key = getattr(settings, 'WEVLIX_API_KEY', '') or ''
    if not key:
        raise WevlixNotConfigured('WEVLIX_API_KEY is not set')
    return key


def _post(path, payload):
    url = f"{settings.WEVLIX_BASE_URL.rstrip('/')}{path}"
    try:
        response = requests.post(
            url,
            json=payload,
            headers={
                'X-API-Key': _api_key(),
                'Content-Type': 'application/json',
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        # انقطاع شبكة أو مهلة — لا ذنب للمستخدم، ولا تفاصيل تُعرض له.
        logger.warning('Wevlix %s unreachable: %s', path, e)
        raise WevlixError('تعذّر إرسال رمز التحقّق، حاول بعد قليل')

    if response.status_code in (200, 201):
        return response.json()

    # 401/403 يعنيان مفتاحاً خاطئاً أو بلا صلاحية — خطؤنا لا خطؤه.
    if response.status_code in (401, 403):
        logger.error('Wevlix rejected our key on %s: %s',
                     path, response.status_code)
        raise WevlixNotConfigured('Wevlix API key rejected')

    logger.warning('Wevlix %s failed %s: %s',
                   path, response.status_code, response.text[:300])
    raise WevlixError('تعذّر إرسال رمز التحقّق، حاول بعد قليل')


def send_otp(phone_e164, client_message_id=None):
    """يطلب من Wevlix توليد رمز وإرساله. يُرجع messageId.

    [client_message_id] مفتاح تكرار (idempotency): إعادة الإرسال بنفس
    المفتاح لا تُنتج رسالة ثانية — وهو ما يحمي من ضغطة مزدوجة تكلّف
    رسالتين.
    """
    payload = {'to': phone_e164}
    if client_message_id:
        payload['clientMessageId'] = client_message_id

    data = _post('/v1/otp/send', payload)
    message_id = data.get('messageId')
    if not message_id:
        logger.error('Wevlix send returned no messageId: %s', data)
        raise WevlixError('تعذّر إرسال رمز التحقّق، حاول بعد قليل')
    return message_id


def verify_otp(message_id, code):
    """True إن قبلت Wevlix الرمز.

    الرمز الخاطئ يردّ 400، وهو جواب لا خطأ — فيُعاد False بدل استثناء
    يُجبر كل مُستدعٍ على التقاطه.
    """
    url = f"{settings.WEVLIX_BASE_URL.rstrip('/')}/v1/otp/verify"
    try:
        response = requests.post(
            url,
            json={'messageId': message_id, 'code': code},
            headers={'X-API-Key': _api_key(), 'Content-Type': 'application/json'},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        logger.warning('Wevlix verify unreachable: %s', e)
        raise WevlixError('تعذّر التحقّق من الرمز، حاول بعد قليل')

    if response.status_code == 200:
        return bool(response.json().get('verified'))
    if response.status_code == 400:
        return False
    if response.status_code in (401, 403):
        logger.error('Wevlix rejected our key on verify: %s', response.status_code)
        raise WevlixNotConfigured('Wevlix API key rejected')

    logger.warning('Wevlix verify failed %s: %s',
                   response.status_code, response.text[:300])
    raise WevlixError('تعذّر التحقّق من الرمز، حاول بعد قليل')
