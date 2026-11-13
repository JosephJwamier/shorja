"""أقدم نسخة من التطبيق ما زال الخادم يخدمها.

نسخة الهاتف لا تُحدَّث وحدها. وحين يتغيّر شكل ردّ الخادم تنهار النسخ
القديمة بخطأ لا يفهمه صاحبها. هذا ما حدث حين انتقل البثّ من Agora إلى
SRS: نسخة تنتظر `app_id` فوصلها null، فرأى المستخدم
«type 'Null' is not a subtype of type 'String'» على شاشة سوداء.

فيسأل التطبيق عند الإقلاع: ما أقدم بنية مدعومة؟ وإن كان أقدم منها يعرض
«حدّث التطبيق» بدل الانهيار.

القياس بـ**رقم البنية** (`+N` في pubspec، وهو versionCode على أندرويد)
لا بالإصدار المقروء `1.2.3`: رقم صحيح يزيد مع كل نسخة، فالمقارنة عملية
طرح واحدة لا تحليل نصوص.

الحدّ صفر افتراضياً، أي لا يُمنع أحد. يُرفع في `.env` بعد أن تصل النسخة
الجديدة إلى المتجر، لا قبل: رفعه قبل ذلك يحجب الجميع عن نسخة لا يجدونها.
"""
from django.conf import settings
from rest_framework.decorators import (
    api_view, authentication_classes, permission_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

_PLATFORMS = ('android', 'ios')


@api_view(['GET'])
# بلا مصادقة: يُسأل قبل تسجيل الدخول، ورمزٌ منتهٍ في نسخة قديمة يجب ألا
# يُخفي عنها أنها قديمة.
@authentication_classes([])
@permission_classes([AllowAny])
def app_version(request):
    platform = request.query_params.get('platform', 'android')
    if platform not in _PLATFORMS:
        platform = 'android'
    if platform == 'ios':
        min_build, store_url = settings.APP_MIN_BUILD_IOS, settings.APP_STORE_URL
    else:
        min_build, store_url = settings.APP_MIN_BUILD_ANDROID, settings.PLAY_STORE_URL
    return Response({
        'platform': platform,
        'min_build': min_build,
        # فارغ قبل النشر في المتجر: يعرض التطبيق الرسالة بلا زرّ بدل زرٍّ
        # يقود إلى صفحة غير موجودة.
        'store_url': store_url,
    })
