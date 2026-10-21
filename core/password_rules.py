"""تشغيل ‎AUTH_PASSWORD_VALIDATORS‎ — ورسائلها بالعربية.

المُدقِّقات الأربعة مُعلَنة في `settings.py` منذ البداية، و`validate_password`
لم تكن تُنادى في أي مسار: لا عند التسجيل، ولا عند تغيير كلمة المرور، ولا عند
استرجاعها بالبريد أو بواتساب. فكل ما فُرض فعلاً هو `min_length=8` في
المُسلسِل — أي أن `12345678` و`password` كانتا مقبولتين على سوق فيه محافظ
ورصيد.

الرسائل تُترجم هنا لا تُمرَّر كما هي: رسائل Django إنجليزية، ومستخدمنا
عربي — و«كلمة المرور مرفوضة» بلا سبب لا تُصلح شيئاً، فالمستخدم يجرّب
كلمةً أخرى بنفس العيب.
"""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

# رموز Django لمُدقِّقاته الأربعة. المطابقة بالرمز لا بالنصّ: النصّ يتغيّر
# بين إصدارات Django، والرمز جزء من عقدها.
_MESSAGES_AR = {
    'password_too_short': 'كلمة المرور يجب أن تكون 8 أحرف على الأقل',
    'password_too_common': 'كلمة مرور شائعة جداً — اختر غيرها',
    'password_entirely_numeric': 'كلمة المرور أرقام فقط — أضف حروفاً',
    'password_too_similar': 'لا تستعمل اسمك أو رقمك أو بريدك',
}

_FALLBACK = 'كلمة المرور ضعيفة — اختر واحدة أقوى'


def password_error(password, user=None):
    """رسالة عربية إن كانت كلمة المرور مرفوضة، أو `None` إن قُبلت.

    [user] يُمرَّر كي يعمل `UserAttributeSimilarityValidator` — بدونه يمرّ
    من يجعل كلمة مروره اسمَه. وعند التسجيل يكون المستخدم غير محفوظ بعد،
    وهذا كافٍ: المُدقِّق يقرأ الحقول من الكائن لا من قاعدة البيانات.
    """
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        # أول سبب لا كلّها: قائمة عيوب تُقرأ كامتحان، وسببٌ واحد يُقرأ
        # كخطوة — وهو نفس اختيار الشريط في التطبيق.
        for error in exc.error_list:
            message = _MESSAGES_AR.get(getattr(error, 'code', None))
            if message:
                return message
        return _FALLBACK
    return None
