"""
The admin site's class, in two layers:

BallaAdminSite gives every admin page the unfold theme (sidebar, colors,
dark mode) — always on. OTPBallaAdminSite adds the hard "must have a
verified OTP session" requirement on top of that, gated behind
settings.ADMIN_REQUIRE_2FA (see core/apps.py) so it only activates once
every staff account has enrolled a device at /account/two_factor/setup/ —
turning it on unconditionally would lock out anyone who hasn't yet.
"""
from django.contrib import admin
from two_factor.admin import AdminSiteOTPRequiredMixin
from unfold.sites import UnfoldAdminSite


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


class BallaAdminSite(UnfoldAdminSite):
    pass


class OTPBallaAdminSite(AdminSiteOTPRequiredMixin, UnfoldAdminSite):
    pass


def configure_admin_site():
    from django.conf import settings
    target_class = OTPBallaAdminSite if settings.ADMIN_REQUIRE_2FA else BallaAdminSite
    admin.site.__class__ = target_class
