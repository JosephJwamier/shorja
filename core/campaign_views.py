"""صفحات الكتيّبات المطبوعة: shorjeh.com/f/<رمز الكتيّب>.

الباركود المطبوع لا يُعاد طبعه، فهو يشير إلى هنا لا إلى المتجر: أين يُحمَّل
التطبيق يُضبط في .env (PLAY_STORE_URL وAPP_STORE_URL وAPK_DOWNLOAD_URL)
ويتغيّر متى شئنا، والورق في أيدي الناس يبقى صالحاً.

والقياس على الخادم لا بسكربت: SHARE_PAGE_CSP تمنع السكربت في كل صفحات
المشاركة. فالزيارة تُسجَّل عند فتح الصفحة، والضغط على التحميل يمرّ
بـ campaign_download الذي يسجّله ثم يحوّل. لا يُخزَّن عنوان IP ولا أيّ شيء
يدلّ على شخص — كعكة واحدة تقول «هذا المتصفّح رأى هذا الكتيّب من قبل».
"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import Campaign, CampaignEvent
from .share_views import SHARE_PAGE_CSP, _locked_down, _store_links

# جالبو المعاينات (واتساب وتيليغرام حين يُرسَل الرابط) ومحرّكات البحث:
# زياراتهم ليست قارئاً مسح ورقة. وواتساب وتيليغرام يعرّفان الجالب باسميهما
# في أوّل الوكيل؛ أمّا متصفّحهما الداخلي فقارئ حقيقي ولا يُستبعد.
_BOTS = re.compile(
    r'bot\b|crawl|spider|facebookexternalhit|^whatsapp|slurp|curl|wget|python-|headless',
    re.I)

# كتيّب الويب وحده يحتاج سكربت التقليب، ومن نطاقنا فقط.
BOOK_CSP = SHARE_PAGE_CSP + "; script-src 'self'"

BOOK_PAGES = range(1, 9)

# الصور الموجودة في static/img/campaigns. اسم آخر يُستبدل بصورة الجمهور:
# static() مع التخزين المُفهرَس ترفع خطأ على ملف غير موجود.
CAMPAIGN_IMAGES = {'c1', 'c2', 'c3', 'm1', 'm2', 'm3'}


def _platform(request):
    agent = request.META.get('HTTP_USER_AGENT', '')
    if re.search(r'iphone|ipad|ipod', agent, re.I):
        return CampaignEvent.IOS
    if re.search(r'android|harmonyos', agent, re.I):
        return CampaignEvent.ANDROID
    return CampaignEvent.OTHER


def _source(request):
    return CampaignEvent.WEB if request.GET.get('s') == 'web' else CampaignEvent.QR


def _links():
    """رابط كل زرّ تحميل، أو '' فيظهر مكانه «قريباً» بدل زرّ ميت."""
    stores = _store_links()
    return {
        'play': stores['play_store_url'],
        'ios': stores['app_store_url'],
        'apk': getattr(settings, 'APK_DOWNLOAD_URL', '') or '',
    }


def _with_referrer(play_url, campaign):
    """رابط Play ومعه مصدر التثبيت.

    يقرؤه Firebase Analytics الموجود في التطبيق من تلقاء نفسه عند أوّل
    تشغيل، فيُنسب التثبيت إلى الكتيّب بلا سطر كود في التطبيق.
    """
    parts = urlsplit(play_url)
    query = dict(parse_qsl(parts.query))
    query['referrer'] = urlencode({
        'utm_source': 'booklet', 'utm_medium': 'print', 'utm_campaign': campaign.code,
    })
    return urlunsplit(parts._replace(query=urlencode(query)))


def _find(code):
    return Campaign.objects.filter(code=code.lower(), is_active=True).first()


def _record(request, campaign, kind, *, target='', is_new=False):
    if request.method != 'GET':
        return
    if _BOTS.search(request.META.get('HTTP_USER_AGENT', '')):
        return
    CampaignEvent.objects.create(
        campaign=campaign, kind=kind, target=target, is_new=is_new,
        platform=_platform(request), source=_source(request),
    )


def _absolute_static(request, path):
    return request.build_absolute_uri(static(path))


@require_safe
@_locked_down
@never_cache
def campaign_landing(request, code):
    campaign = _find(code)
    if campaign is None:
        # ورقة قديمة أو رمز قُرئ خطأً: صفحة التحميل العامة لا 404.
        return redirect('share-get-app')

    seen_cookie = f'seen_{campaign.code}'
    _record(request, campaign, CampaignEvent.SCAN,
            is_new=seen_cookie not in request.COOKIES)

    links = _links()
    image = campaign.image_name
    if image not in CAMPAIGN_IMAGES:
        image = 'm1' if campaign.audience == Campaign.MERCHANT else 'c1'
    response = render(request, 'share/campaign.html', {
        'campaign': campaign,
        'points': [p.strip() for p in campaign.points.splitlines() if p.strip()],
        'platform': _platform(request),
        'links': links,
        'any_link': any(links.values()),
        'source_query': '?s=web' if _source(request) == CampaignEvent.WEB else '',
        'canonical': request.build_absolute_uri(request.path),
        'image_url': _absolute_static(request, f'img/campaigns/{image}.jpg'),
        'icon_url': _absolute_static(request, 'img/app-icon.png'),
    })
    response.set_cookie(seen_cookie, '1', max_age=180 * 24 * 3600, path='/f/',
                        httponly=True, samesite='Lax', secure=request.is_secure())
    return response


@require_safe
@never_cache
def campaign_download(request, code, target):
    """يسجّل الضغطة ثم يحوّل. العناوين من .env وحدها، فلا تحويل مفتوح."""
    campaign = _find(code)
    if campaign is None:
        return redirect('share-get-app')
    url = _links().get(target)
    if not url:
        return redirect('campaign-landing', code=campaign.code)
    _record(request, campaign, CampaignEvent.CLICK, target=target)
    if target == 'play':
        url = _with_referrer(url, campaign)
    return HttpResponseRedirect(url)


@require_safe
@never_cache
def campaign_book(request, code):
    """الكتيّب نفسه يُقلَّب في المتصفّح — لمن يصله الرابط لا الورقة."""
    campaign = _find(code)
    if campaign is None or not campaign.has_book:
        return redirect('share-get-app')
    pages = [_absolute_static(request, f'flipbooks/{campaign.code}/{n}.webp')
             for n in BOOK_PAGES]
    response = render(request, 'share/book.html', {
        'campaign': campaign,
        'pages': pages,
        'cover_url': pages[0],
        'script_url': _absolute_static(request, 'js/flipbook.js'),
        'icon_url': _absolute_static(request, 'img/app-icon.png'),
        'canonical': request.build_absolute_uri(request.path),
    })
    response['Content-Security-Policy'] = BOOK_CSP
    return response
