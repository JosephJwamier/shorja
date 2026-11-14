"""صفحات المعاينة لروابط المشاركة (shorjeh.com/p/… /s/… /a/… /l/…).

لماذا تُخدم من الخادم أصلاً، ومعها تطبيق مثبّت على الهاتف؟ لأن الرابط يمرّ
بمرحلتين لا واحدة:

1. **قبل أن يُضغط** — واتساب وتيليغرام يجلبان الرابط بأنفسهما ليرسما البطاقة.
   هما لا يملكان التطبيق ولا حساباً، ويقرآن وسوم Open Graph فقط. رابط بلا
   `og:image` يظهر نصاً أزرق أصمّ، فينخفض الضغط عليه حتى ممّن عنده التطبيق.

2. **بعد أن يُضغط** — إن كان التطبيق مثبّتاً و`assetlinks.json` منشوراً،
   يعترض أندرويد الرابط قبل أن يصل المتصفّح، فلا تُطلب هذه الصفحة إطلاقاً.
   وإن لم يكن، فهذه الصفحة هي ما يراه الزائر: معاينة حقيقية وزرّ تحميل.

أي أن هذه الصفحات تُخدم لمن لا تطبيق عنده — وهم بالضبط من نريد كسبهم.

كلها عامة (AllowAny ضمناً: دوال Django عادية لا DRF) وللقراءة فقط.
"""
import functools

from django.http import Http404
from django.shortcuts import render
from django.conf import settings
from django.templatetags.static import static
from django.views.decorators.cache import cache_control

from .models import AuctionListing, FixedPriceListing, LiveSession, UserStore

# صورة تُعرض حين لا صورة للمنتج. واتساب يرفض البطاقة بلا og:image، فبديل
# ثابت أفضل من وسم ناقص.
FALLBACK_IMAGE = '/static/share-fallback.png'

# لا صفحة هنا تشغّل سكربتاً — بطاقات Open Graph وصفحة تحميل — فالسياسة
# تمنع السكربت كلياً. القوالب تهرّب كل ما يكتبه البائع (العنوان والوصف)،
# وهذه الطبقة هي ما يبقى يوم يفلت شيء منها: الوسم المحقون يظهر نصاً ولا
# يعمل أبداً. وما تحمّله الصفحات من خارجها: أنماط <style> المضمّنة، وخطوط
# Google في home.html، وصور https (صور المنتجات من نطاق الوسائط أو R2).
SHARE_PAGE_CSP = '; '.join([
    "default-src 'none'",
    "img-src 'self' https: data:",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])


def _locked_down(view):
    @functools.wraps(view)
    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response['Content-Security-Policy'] = SHARE_PAGE_CSP
        return response
    return wrapped

def _store_links():
    """رابطا المتجرين، أو '' لكلٍّ منهما قبل النشر.

    من البيئة لا ثابتين في الكود: يوم ينشر التطبيق يصير الرابط معروفاً، وربطه
    بإعادة بناء الحاوية ونشرٍ كامل لأجل سلسلة نصّية واحدة ثمنٌ لا داعي له.
    فارغاً يختفي الزرّ ويظهر «قادم قريباً» بدل أن يقود إلى 404.
    """
    return {
        'play_store_url': getattr(settings, 'PLAY_STORE_URL', '') or '',
        'app_store_url': getattr(settings, 'APP_STORE_URL', '') or '',
    }


def _absolute(request, path):
    """رابط مطلق على نطاق المشاركة.

    واتساب يجلب og:image من خادمه هو، لا من هاتف القارئ، فالمسار النسبي
    لا يعني له شيئاً.
    """
    if not path:
        return ''
    if path.startswith('http://') or path.startswith('https://'):
        return path
    return request.build_absolute_uri(path)


def _render(request, *, title, description, image, deep_link, meta=None):
    return render(request, 'share/preview.html', {
        'title': title,
        'description': (description or '')[:200],
        'image': _absolute(request, image or FALLBACK_IMAGE),
        'canonical': request.build_absolute_uri(),
        'deep_link': deep_link,
        'meta': meta or [],
    })


def _first_image(item):
    image = item.images.order_by('order', 'id').first()
    return image.image.url if image and image.image else None


def _item_title(item):
    # الاسم العربي هو ما يفهمه القارئ العراقي؛ title إنجليزي في الغالب.
    return item.name_ar or item.title


@_locked_down
@cache_control(max_age=300)
def product(request, listing_id):
    listing = (
        FixedPriceListing.objects
        .select_related('item', 'item__owner')
        .prefetch_related('item__images')
        .filter(pk=listing_id, is_active=True)
        .first()
    )
    if listing is None:
        raise Http404
    item = listing.item

    return _render(
        request,
        title=_item_title(item),
        description=item.description,
        image=_first_image(item),
        deep_link=f'/p/{listing_id}',
        meta=[('السعر', f'{listing.price:,.0f} د.ع')],
    )


@_locked_down
@cache_control(max_age=300)
def store(request, store_id):
    shop = UserStore.objects.filter(pk=store_id, is_active=True).first()
    if shop is None:
        raise Http404

    return _render(
        request,
        title=shop.name,
        description=shop.description,
        image=shop.logo.url if shop.logo else (
            shop.cover_image.url if shop.cover_image else None),
        deep_link=f'/s/{store_id}',
    )


@_locked_down
@cache_control(max_age=60)
def auction(request, auction_id):
    # مهلة أقصر من المنتج: السعر الحالي يتغيّر مع كل مزايدة، وبطاقة
    # محفوظة خمس دقائق تعرض رقماً تجاوزه المزاد.
    listing = (
        AuctionListing.objects
        .select_related('item')
        .prefetch_related('item__images')
        .filter(pk=auction_id)
        .first()
    )
    if listing is None:
        raise Http404
    item = listing.item

    return _render(
        request,
        title=f'مزاد: {_item_title(item)}',
        description=item.description,
        image=_first_image(item),
        deep_link=f'/a/{auction_id}',
        meta=[('السعر الحالي', f'{listing.current_price:,.0f} د.ع')],
    )


@_locked_down
@cache_control(max_age=60)
def live(request, session_id):
    session = (
        LiveSession.objects.select_related('store').filter(pk=session_id).first()
    )
    if session is None:
        raise Http404

    store_name = session.store.name if session.store else ''
    thumb = session.thumbnail.url if session.thumbnail else (
        session.store.logo.url if session.store and session.store.logo else None)

    return _render(
        request,
        title=session.title or 'بث مباشر',
        description=session.description or (
            f'بث مباشر من {store_name}' if store_name else 'بث مباشر'),
        image=thumb,
        deep_link=f'/l/{session_id}',
        meta=[('المتجر', store_name)] if store_name else None,
    )


@_locked_down
@cache_control(max_age=3600)
def get_app(request):
    """صفحة التحميل — وجهة كل زرّ يقول «افتح في التطبيق» أو «حمّل التطبيق».

    صفحةٌ لا رابطٌ مباشر إلى Google Play، لأن الزرّ واحد والقارئ قد يكون
    على أيّ من المنصّتين: رابط Play يصل صاحب الآيفون إلى صفحة لا تنفعه.
    وهي أيضاً الموضع الوحيد الذي يعرف أين يُحمَّل التطبيق، فيوم يُنشر على
    متجر ثانٍ لا يُبحث عن الأزرار في القوالب.

    وقبل النشر لا تُخفى الصفحة بل تقول «قادم قريباً» لكل منصّة لم تُضبط:
    زرٌّ يقود إلى 404 أسوأ من وعدٍ صريح.
    """
    return render(request, 'share/get.html', {
        'canonical': request.build_absolute_uri(),
        # مطلقاً لا نسبياً: STATIC_URL هنا بلا شرطة بادئة، فـ{% static %}
        # يُخرج مساراً نسبياً يُحلّ بحسب عمق الصفحة — وهو ما يحتاجه
        # og:image مطلقاً على أي حال، فالجالب على خادم واتساب.
        'icon_url': request.build_absolute_uri(static('img/app-icon.png')),
        **_store_links(),
    })


@_locked_down
@cache_control(max_age=3600)
def home(request):
    """جذر shorjeh.com.

    كان غير مسجَّل في urls.py فيردّ 404 — وهو أول ما يراه من يفتح النطاق،
    ومن يحذف جزءاً من رابط مشارَك، ومن يفحصه قبل تثبيت التطبيق.

    قالب منفصل عن preview.html لا نفسه: صفحة المعاينة مبنية حول صورة
    منتج وزرّ واحد، والجذر يحتاج أن يشرح ما هذا المكان.
    """
    return render(request, 'share/home.html', {
        'canonical': request.build_absolute_uri(),
        **_store_links(),
    })
