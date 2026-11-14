"""
The admin site's class, in two layers:

BallaAdminSite gives every admin page the unfold theme (sidebar, colors,
dark mode) — always on. OTPBallaAdminSite adds the hard "must have a
verified OTP session" requirement on top of that, gated behind
settings.ADMIN_REQUIRE_2FA (see core/apps.py), which production always has on.

Enrolment — binding an authenticator app to a staff account — happens only on
the server, with `manage.py enroll_admin_2fa <username>`. On the web the only
proof of identity at that moment is the password, so whoever knew it first
could bind *their* phone and own the panel from then on (trust on first use).
Shell access to the server is a proof the password alone cannot give.
two_factor's own setup pages are therefore shadowed in balla/urls.py by
enrol_on_server below.
"""
from django.contrib import admin
from django.http import HttpResponse
from django.utils.html import format_html
from two_factor.admin import AdminSiteOTPRequiredMixin
from two_factor.utils import default_device
from unfold.sites import UnfoldAdminSite

ENROL_COMMAND = (
    'docker compose -f deploy/docker-compose.yml --env-file .env '
    'exec web python manage.py enroll_admin_2fa {username}'
)


def pending_installment_requests(request):
    """الرقم الذي يظهر بجانب «طلبات التقسيط» في القائمة الجانبية.

    كل طلب هنا مشترٍ ينتظر مكالمة خلال 72 ساعة، ولا بريد يُنبّه أحداً
    (البريد معطّل على مستوى المنصة) — فالعدّاد في القائمة هو ما يجعل
    الطلب مرئياً من أي صفحة في اللوحة بدل أن ينام حتى يفتح أحدٌ القائمة
    صدفةً.

    يُنادى عند رسم كل صفحة إدارة: استعلام COUNT واحد، ويُبتلع خطؤه —
    لوحة الإدارة لا يجوز أن تسقط لأن عدّاداً فشل (أثناء ترحيل مثلاً،
    والجدول لم يُنشأ بعد).
    """
    from core.models import InstallmentPlan
    try:
        count = InstallmentPlan.objects.filter(
            state=InstallmentPlan.STATE_REQUESTED).count()
    except Exception:
        return ''
    return str(count) if count else ''


def enrol_on_server(request):
    """What a staff member without an authenticator sees instead of a QR code.

    403, not a redirect to a setup wizard: nothing on the web may bind a
    device. The page names the one command that does.
    """
    user = getattr(request, 'user', None)
    username = user.get_username() if user is not None and user.is_authenticated else '<username>'
    body = format_html(
        '<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>التحقق بخطوتين</title></head>'
        '<body style="font-family:system-ui,Tahoma,sans-serif;max-width:640px;'
        'margin:48px auto;padding:0 16px;line-height:1.8">'
        '<h1 style="font-size:1.3rem">لوحة الإدارة تتطلب التحقق بخطوتين</h1>'
        '<p>هذا الحساب لم يربط تطبيق مصادقة بعد. الربط لا يتم من المتصفح، '
        'لأن كلمة المرور وحدها لا تثبت أنك صاحب الحساب. نفّذ هذا الأمر على '
        'الخادم من مجلد المشروع، ثم امسح الرمز بتطبيق المصادقة:</p>'
        '<pre dir="ltr" style="background:#f2f3f5;padding:12px;border-radius:6px;'
        'white-space:pre-wrap;word-break:break-all">{}</pre>'
        '<p>بعدها سجّل الدخول من جديد وأدخل الرمز المكوّن من 6 أرقام.</p>'
        '</body></html>',
        ENROL_COMMAND.format(username=username),
    )
    return HttpResponse(body, status=403)


class BallaAdminSite(UnfoldAdminSite):
    pass


class OTPBallaAdminSite(AdminSiteOTPRequiredMixin, UnfoldAdminSite):
    def login(self, request, extra_context=None):
        # A staff session that passed the password step but has no device to
        # verify with would bounce between the admin and the login form
        # forever. Tell it the way out instead.
        user = request.user
        if (user.is_authenticated and user.is_active and user.is_staff
                and not user.is_verified() and default_device(user) is None):
            return enrol_on_server(request)
        return super().login(request, extra_context)


def configure_admin_site():
    from django.conf import settings
    target_class = OTPBallaAdminSite if settings.ADMIN_REQUIRE_2FA else BallaAdminSite
    admin.site.__class__ = target_class
