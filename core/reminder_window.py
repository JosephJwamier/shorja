"""أوقات إرسال التذكيرات — نافذة الدوام.

رسالة واتساب الساعة الثالثة فجراً لا تُقرأ عند وصولها، لكنها توقظ صاحبها.
وتاجرٌ أيقظه إشعارٌ من تطبيقنا مرّةً يُسكت الرقم أو يحظره — فنخسر معه
إشعارات الحجز والمزاد أيضاً، وهي التي تُدرّ عليه مالاً.

فالنافذة ليست تهذيباً بل حمايةً لقناة الاتصال نفسها.

الدوالّ هنا خالصة وتعمل على أوقات واعية بالمنطقة الزمنية. والمنطقة بغداد
(`TIME_ZONE` في الإعدادات) لا UTC: «التاسعة صباحاً» تعني التاسعة عند
التاجر، وهي ثلاث ساعات إزاحة تقلب الليل نهاراً لو حُسبت بـUTC.
"""
from datetime import timedelta


def is_within_window(moment, *, start_hour, end_hour):
    """هل [moment] داخل نافذة الدوام؟

    [moment] يجب أن يكون بالتوقيت المحلّي للتاجر لا بـUTC.
    """
    return start_hour <= moment.hour < end_hour


def next_window_opening(moment, *, start_hour, end_hour):
    """أوّل لحظة داخل النافذة عند [moment] أو بعدها.

    يُرجع [moment] نفسه إن كان داخلها أصلاً، فلا يؤخّر ما يمكن إرساله
    الآن.

    الحالات الثلاث التي يفصلها:
      قبل الفتح  → فتح اليوم نفسه
      داخلها     → الآن
      بعد الإغلاق → فتح الغد
    """
    if is_within_window(moment, start_hour=start_hour, end_hour=end_hour):
        return moment

    opening = moment.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if moment.hour >= end_hour:
        # بعد الإغلاق: الفتحة التالية غداً. وقبل الفتح: اليوم.
        opening += timedelta(days=1)
    return opening


def seconds_until_window(moment, *, start_hour, end_hour):
    """كم ثانية حتى يُصبح الإرسال مسموحاً. صفر إن كان مسموحاً الآن."""
    opening = next_window_opening(moment, start_hour=start_hour, end_hour=end_hour)
    return max(int((opening - moment).total_seconds()), 0)
