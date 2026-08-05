from decimal import Decimal, InvalidOperation

from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q, Sum
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils import timezone
from import_export import resources
from import_export.admin import ExportActionMixin
from unfold.admin import ModelAdmin, TabularInline
from unfold.contrib.import_export.forms import SelectableFieldsExportForm

from .models import (
    User, Location, UserStore, Representative,
    Category, Item, ItemImage, ItemLocationHistory,
    FixedPriceListing, AuctionListing, Bid, ItemTransfer, StorageFeePayment,
    Reservation, Transaction, Dispute, Report,
    Rating, ProductReview, StoreReview, Chat, Message,
    StoreFollow, StoreCoupon, CouponCollection,
    LiveSession, LiveSessionItem, LiveSessionLike, LiveSessionComment,
    DeliveryCompany, DeliveryOrder, CashSettlement, DeliveryBatch,
    Wallet, Notification, DepositRequest, WithdrawalRequest,
)
from .services.disputes import resolve_dispute, DisputeResolutionError
from .services.withdrawals import approve_withdrawal, reject_withdrawal, WithdrawalReviewError
from .services.moderation import (
    archive_item, reactivate_user, stop_live_session, suspend_user,
    resolve_report, ReportResolutionError,
)
from .services.support import SUPPORT_USERNAME
from .notifications import notify_report_reviewed


# =============================================================================
# تصدير التقارير (Phase 7) — resources referenced by the ExportActionMixin
# on UserAdmin/ReservationAdmin/TransactionAdmin/DisputeAdmin below. Kept
# read-only exports only (no ImportMixin) — nobody should be able to
# bulk-overwrite users/reservations/transactions/disputes via CSV upload.
# =============================================================================

class UserExportResource(resources.ModelResource):
    class Meta:
        model = User
        fields = (
            'id', 'username', 'email', 'phone', 'is_active', 'is_staff',
            'identity_verified', 'phone_verified', 'email_verified',
            'trust_score', 'total_sales', 'total_purchases',
            'date_joined', 'created_at',
        )
        export_order = fields


class ReservationExportResource(resources.ModelResource):
    class Meta:
        model = Reservation
        fields = ('id', 'item', 'buyer', 'quantity', 'total_price', 'status',
                   'reserved_at', 'completed_at', 'warranty_days')
        export_order = fields

    def dehydrate_item(self, obj):
        return obj.item.title

    def dehydrate_buyer(self, obj):
        return obj.buyer.username


class TransactionExportResource(resources.ModelResource):
    class Meta:
        model = Transaction
        fields = ('id', 'user', 'transaction_type', 'amount', 'status', 'created_at', 'completed_at')
        export_order = fields

    def dehydrate_user(self, obj):
        return obj.user.username


class DisputeExportResource(resources.ModelResource):
    class Meta:
        model = Dispute
        fields = ('id', 'title', 'item', 'complainant', 'defendant', 'status',
                   'resolution_action', 'created_at', 'resolved_at')
        export_order = fields

    def dehydrate_item(self, obj):
        return obj.item.title

    def dehydrate_complainant(self, obj):
        return obj.complainant.username

    def dehydrate_defendant(self, obj):
        return obj.defendant.username


# =============================================================================
# المستخدمون والمتاجر
# =============================================================================

# Not auto-registered by Django since this is a custom AUTH_USER_MODEL —
# needed so an admin can create a plain login (e.g. a delivery company's
# account) from the admin without going through the public registration
# flow. Combines Django's own field layout (password change, permissions
# widgets) with the unfold theme.
@admin.register(User)
class UserAdmin(DjangoUserAdmin, ExportActionMixin, ModelAdmin):
    """
    DjangoUserAdmin's stock fieldsets/list_display only know about the
    built-in auth fields — trust_score, identity_verified, admin_notes etc.
    were invisible in the admin until extended here. Suspending a user
    (is_active=False) with a reason goes through suspend_users_action below
    rather than the plain checkbox, so the reason always gets recorded.
    """
    fieldsets = DjangoUserAdmin.fieldsets + (
        ('الثقة والتحقق', {'fields': (
            'phone', 'phone_verified', 'email_verified', 'identity_verified',
            'trust_score', 'total_sales', 'total_purchases', 'admin_notes',
        )}),
    )
    readonly_fields = DjangoUserAdmin.readonly_fields + ('trust_score', 'total_sales', 'total_purchases')
    list_display = DjangoUserAdmin.list_display + ('is_active', 'identity_verified')
    list_filter = DjangoUserAdmin.list_filter + ('identity_verified', 'phone_verified')
    actions = [
        'verify_identity_action', 'reactivate_users_action', 'suspend_users_action',
        'send_notification_action',
    ]
    resource_classes = [UserExportResource]
    export_form_class = SelectableFieldsExportForm

    @admin.action(description='توثيق الهوية للمحددين', permissions=['change'])
    def verify_identity_action(self, request, queryset):
        count = 0
        for user in queryset.filter(identity_verified=False):
            user.identity_verified = True
            user.save(update_fields=['identity_verified'])
            count += 1
        self.message_user(request, f'تم توثيق هوية {count} مستخدم.', level=messages.SUCCESS)

    @admin.action(description='إعادة تفعيل الحسابات المحددة', permissions=['change'])
    def reactivate_users_action(self, request, queryset):
        count = 0
        for user in queryset.filter(is_active=False):
            reactivate_user(user, actor=request.user)
            count += 1
        self.message_user(request, f'أُعيد تفعيل {count} حساب.', level=messages.SUCCESS)

    @admin.action(description='تعليق الحسابات المحددة (بسبب)', permissions=['change'])
    def suspend_users_action(self, request, queryset):
        if 'apply' in request.POST:
            ids = request.POST.getlist(ACTION_CHECKBOX_NAME)
            selected = User.objects.filter(pk__in=ids)
            reason = request.POST.get('reason', '').strip()

            if not reason:
                self.message_user(request, 'سبب التعليق إلزامي.', level=messages.ERROR)
                return TemplateResponse(request, 'admin/core/user/suspend_confirmation.html', dict(
                    self.admin_site.each_context(request), title='تعليق الحسابات المحددة',
                    queryset=selected, opts=self.model._meta, action_checkbox_name=ACTION_CHECKBOX_NAME,
                ))

            count = 0
            for user in selected:
                if user.pk == request.user.pk:
                    continue  # never let an admin lock themselves out by accident
                suspend_user(user, reason=reason, actor=request.user)
                count += 1
            self.message_user(request, f'تم تعليق {count} حساب.', level=messages.SUCCESS)
            return None

        context = dict(
            self.admin_site.each_context(request),
            title='تعليق الحسابات المحددة',
            queryset=queryset,
            opts=self.model._meta,
            action_checkbox_name=ACTION_CHECKBOX_NAME,
        )
        return TemplateResponse(request, 'admin/core/user/suspend_confirmation.html', context)

    @admin.action(description='إرسال إشعار للمستخدمين المحددين', permissions=['change'])
    def send_notification_action(self, request, queryset):
        if 'apply' in request.POST:
            ids = request.POST.getlist(ACTION_CHECKBOX_NAME)
            selected = User.objects.filter(pk__in=ids)
            title = request.POST.get('notif_title', '').strip()
            body = request.POST.get('notif_body', '').strip()

            if not title or not body:
                self.message_user(request, 'العنوان والنص إلزاميان.', level=messages.ERROR)
                return TemplateResponse(request, 'admin/core/user/send_notification.html', dict(
                    self.admin_site.each_context(request), title='إرسال إشعار للمستخدمين المحددين',
                    queryset=selected, opts=self.model._meta, action_checkbox_name=ACTION_CHECKBOX_NAME,
                ))

            from .notifications import notify_push

            count = 0
            for user in selected:
                notify_push(user, title, body, data={'type': 'general'})
                count += 1
            self.message_user(request, f'تم إرسال الإشعار إلى {count} مستخدم.', level=messages.SUCCESS)
            return None

        context = dict(
            self.admin_site.each_context(request),
            title='إرسال إشعار للمستخدمين المحددين',
            queryset=queryset,
            opts=self.model._meta,
            action_checkbox_name=ACTION_CHECKBOX_NAME,
        )
        return TemplateResponse(request, 'admin/core/user/send_notification.html', context)


@admin.register(Location)
class LocationAdmin(ModelAdmin):
    list_display = ('name', 'user', 'location_type', 'city', 'state', 'is_primary', 'is_active')
    list_filter = ('location_type', 'state', 'is_active')
    search_fields = ('name', 'city', 'contact_person', 'contact_phone', 'user__username')
    raw_id_fields = ('user',)
    list_select_related = ('user',)


@admin.register(UserStore)
class UserStoreAdmin(ModelAdmin):
    list_display = ('name', 'user', 'is_active', 'rating', 'phone_number', 'whatsapp_number', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'user__username', 'phone_number', 'whatsapp_number')
    raw_id_fields = ('user', 'default_location')
    list_select_related = ('user',)
    actions = ['deactivate_stores_action', 'activate_stores_action']

    @admin.action(description='تعطيل المتاجر المحددة', permissions=['change'])
    def deactivate_stores_action(self, request, queryset):
        count = queryset.filter(is_active=True).update(is_active=False)
        self.message_user(request, f'تم تعطيل {count} متجر.', level=messages.SUCCESS)

    @admin.action(description='تفعيل المتاجر المحددة', permissions=['change'])
    def activate_stores_action(self, request, queryset):
        count = queryset.filter(is_active=False).update(is_active=True)
        self.message_user(request, f'تم تفعيل {count} متجر.', level=messages.SUCCESS)


@admin.register(StoreFollow)
class StoreFollowAdmin(ModelAdmin):
    list_display = ('follower', 'store', 'created_at')
    search_fields = ('follower__username', 'store__name')
    raw_id_fields = ('follower', 'store')
    list_select_related = ('follower', 'store', 'store__user')


@admin.register(Representative)
class RepresentativeAdmin(ModelAdmin):
    list_display = ('item', 'owner', 'representative', 'status', 'commission_percentage', 'created_at')
    list_filter = ('status',)
    search_fields = ('item__title', 'owner__username', 'representative__username')
    raw_id_fields = ('item', 'owner', 'representative')
    list_select_related = ('item', 'item__owner', 'owner', 'representative')


# =============================================================================
# المنتجات والتصنيفات
# =============================================================================

@admin.register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ('thumbnail', 'name_ar', 'name', 'parent', 'order', 'is_active')
    list_display_links = ('name_ar', 'name')
    list_editable = ('order', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'name_ar', 'slug')
    autocomplete_fields = ('parent',)
    prepopulated_fields = {'slug': ('name',)}

    @admin.display(description='الصورة')
    def thumbnail(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="height:32px;width:32px;object-fit:cover;'
                'border-radius:6px;" />',
                obj.image.url,
            )
        return '—'


class ItemImageInline(TabularInline):
    model = ItemImage
    extra = 0
    fields = ('preview', 'image', 'order')
    readonly_fields = ('preview',)

    @admin.display(description='معاينة')
    def preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="height:48px;width:48px;object-fit:cover;'
                'border-radius:6px;" />',
                obj.image.url,
            )
        return '—'


@admin.register(Item)
class ItemAdmin(ModelAdmin):
    list_display = ('title', 'owner', 'category', 'status', 'condition', 'quantity', 'warranty_days', 'sold_count', 'rating', 'created_at')
    list_filter = ('status', 'condition', 'category', 'is_daily_deal')
    search_fields = ('title', 'name_ar', 'owner__username')
    raw_id_fields = ('owner', 'current_holder', 'current_location', 'category')
    list_select_related = ('owner', 'category')
    inlines = [ItemImageInline]
    date_hierarchy = 'created_at'
    actions = ['archive_selected_items']

    @admin.action(description='أرشفة/إخفاء المنتجات المحددة (بسبب)', permissions=['change'])
    def archive_selected_items(self, request, queryset):
        if 'apply' in request.POST:
            ids = request.POST.getlist(ACTION_CHECKBOX_NAME)
            selected = Item.objects.filter(pk__in=ids)
            reason = request.POST.get('reason', '').strip()

            if not reason:
                self.message_user(request, 'سبب الأرشفة إلزامي.', level=messages.ERROR)
                return TemplateResponse(request, 'admin/core/item/archive_confirmation.html', dict(
                    self.admin_site.each_context(request), title='أرشفة المنتجات المحددة',
                    queryset=selected, opts=self.model._meta, action_checkbox_name=ACTION_CHECKBOX_NAME,
                ))

            count = 0
            for item in selected.exclude(status='archived'):
                archive_item(item, reason=reason, actor=request.user)
                count += 1
            self.message_user(request, f'تمت أرشفة {count} منتج.', level=messages.SUCCESS)
            return None

        context = dict(
            self.admin_site.each_context(request),
            title='أرشفة المنتجات المحددة',
            queryset=queryset,
            opts=self.model._meta,
            action_checkbox_name=ACTION_CHECKBOX_NAME,
        )
        return TemplateResponse(request, 'admin/core/item/archive_confirmation.html', context)

    def has_delete_permission(self, request, obj=None):
        # Item is CASCADE-referenced by Reservation, ItemTransfer, Dispute,
        # DeliveryOrder (via Reservation), ProductReview, Chat, etc. — a hard
        # delete from here would silently wipe that history. Use
        # "أرشفة المنتجات المحددة" above instead (same status='archived' path
        # the app's own delete button uses via archive_item()).
        return False


@admin.register(ItemLocationHistory)
class ItemLocationHistoryAdmin(ModelAdmin):
    """Auto-created by Item.update_location() — a history log, not something
    an admin should hand-author, so this stays view-only."""
    list_display = ('item', 'location', 'moved_by', 'moved_at')
    list_filter = ('moved_at',)
    search_fields = ('item__title',)
    raw_id_fields = ('item', 'location', 'moved_by')
    list_select_related = ('item', 'item__owner', 'location', 'moved_by')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# =============================================================================
# البيع والمزادات
# =============================================================================

@admin.register(FixedPriceListing)
class FixedPriceListingAdmin(ModelAdmin):
    list_display = ('item', 'price', 'original_price', 'deposit_percentage', 'deposit_amount', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('item__title',)
    raw_id_fields = ('item',)
    list_select_related = ('item', 'item__owner')


class BidInline(TabularInline):
    model = Bid
    extra = 0
    fields = ('bidder', 'amount', 'deposit_paid', 'deposit_refunded', 'is_winning', 'created_at')
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(AuctionListing)
class AuctionListingAdmin(ModelAdmin):
    list_display = ('item', 'status', 'starting_price', 'current_price', 'minimum_increment', 'start_time', 'end_time', 'winner')
    list_filter = ('status',)
    search_fields = ('item__title',)
    raw_id_fields = ('item', 'winner')
    list_select_related = ('item', 'item__owner', 'winner')
    inlines = [BidInline]


@admin.register(ItemTransfer)
class ItemTransferAdmin(ModelAdmin):
    list_display = ('item', 'from_user', 'to_user', 'status', 'quantity', 'wholesale_price_per_unit', 'storage_fee_type', 'initiated_at')
    list_filter = ('status', 'storage_fee_type')
    search_fields = ('item__title', 'from_user__username', 'to_user__username')
    raw_id_fields = ('item', 'from_user', 'to_user', 'resulting_item')
    list_select_related = ('item', 'item__owner', 'from_user', 'to_user')


@admin.register(StorageFeePayment)
class StorageFeePaymentAdmin(ModelAdmin):
    list_display = ('transfer', 'amount', 'period_start', 'period_end', 'due_date', 'status', 'paid_at')
    list_filter = ('status',)
    search_fields = ('transfer__item__title',)
    raw_id_fields = ('transfer',)
    list_select_related = ('transfer', 'transfer__item', 'transfer__from_user', 'transfer__to_user')


# =============================================================================
# الحجوزات والمعاملات
# =============================================================================

@admin.register(Reservation)
class ReservationAdmin(ExportActionMixin, ModelAdmin):
    list_display = ('id', 'item', 'buyer', 'quantity', 'total_price', 'status', 'is_deposit_only', 'warranty_days', 'reserved_at', 'expires_at')
    list_filter = ('status', 'is_deposit_only')
    search_fields = ('item__title', 'buyer__username')
    raw_id_fields = (
        'item', 'buyer', 'representative', 'representative_assignment',
        'related_transfer', 'delivery_location', 'applied_coupon',
        'applied_offer', 'live_session', 'live_session_item', 'auction',
    )
    date_hierarchy = 'reserved_at'
    list_select_related = ('item', 'item__owner', 'buyer')
    resource_classes = [ReservationExportResource]
    export_form_class = SelectableFieldsExportForm

    def has_delete_permission(self, request, obj=None):
        # Reservation is CASCADE-referenced by Dispute and DeliveryOrder — a
        # hard delete from here would silently wipe that history. Cancel it
        # instead (status='cancelled'), same as ReservationViewSet.cancel.
        return False


@admin.register(Transaction)
class TransactionAdmin(ExportActionMixin, ModelAdmin):
    list_display = ('id', 'user', 'transaction_type', 'amount', 'status', 'created_at', 'completed_at')
    list_filter = ('transaction_type', 'status')
    search_fields = ('user__username', 'stripe_payment_intent_id')
    raw_id_fields = ('user', 'item', 'reservation', 'auction', 'transfer')
    date_hierarchy = 'created_at'
    list_select_related = ('user',)
    resource_classes = [TransactionExportResource]
    export_form_class = SelectableFieldsExportForm


# =============================================================================
# مركز حل النزاعات
# =============================================================================

_DISPUTE_BADGE_COLORS = {
    'open': ('#AC3A31', '#AC3A3120'),
    'investigating': ('#9C6216', '#9C621620'),
    'resolved': ('#2E7D57', '#2E7D5720'),
    'closed': ('#6B7280', '#6B728020'),
}


@admin.register(Dispute)
class DisputeAdmin(ExportActionMixin, ModelAdmin):
    """
    Resolution (refund_buyer/pay_seller/partial_refund/no_action) only
    happens through resolve_view below, which calls the same
    resolve_dispute() service the DRF API uses — status/resolution/
    resolution_action stay read-only here so a plain field edit can never
    move money without going through that guarded path. admin_notes is
    safe to jot down directly.
    """
    list_display = ('id', 'title', 'item', 'complainant', 'defendant', 'status_badge', 'sla_badge', 'created_at', 'resolve_link')
    list_filter = ('status', 'reason', 'created_at')
    search_fields = ('title', 'item__title', 'complainant__username', 'defendant__username')
    raw_id_fields = ('item', 'reservation', 'complainant', 'defendant')
    list_select_related = ('item', 'item__owner', 'complainant', 'defendant')
    resource_classes = [DisputeExportResource]
    export_form_class = SelectableFieldsExportForm
    readonly_fields = (
        'item', 'reservation', 'complainant', 'defendant', 'title', 'description',
        'reason', 'status', 'resolution', 'resolution_action', 'created_at', 'resolved_at',
    )

    def has_add_permission(self, request):
        return False

    @admin.display(description='الحالة')
    def status_badge(self, obj):
        color, bg = _DISPUTE_BADGE_COLORS.get(obj.status, ('#6B7280', '#6B728020'))
        return format_html(
            '<span style="background:{};color:{};padding:2px 10px;border-radius:999px;'
            'font-size:12px;font-weight:600;white-space:nowrap;">{}</span>',
            bg, color, obj.get_status_display(),
        )

    @admin.display(description='مهلة الاستجابة')
    def sla_badge(self, obj):
        if obj.status not in ('open', 'investigating'):
            return '—'
        age_days = (timezone.now() - obj.created_at).days
        if age_days >= 2:
            color, bg, label = '#AC3A31', '#AC3A3120', f'متأخر — {age_days} يوم'
        elif age_days >= 1:
            color, bg, label = '#9C6216', '#9C621620', 'منذ يوم'
        else:
            color, bg, label = '#2E7D57', '#2E7D5720', 'اليوم'
        return format_html(
            '<span style="background:{};color:{};padding:2px 10px;border-radius:999px;'
            'font-size:12px;font-weight:600;white-space:nowrap;">{}</span>',
            bg, color, label,
        )

    @admin.display(description='')
    def resolve_link(self, obj):
        if obj.status in ('resolved', 'closed'):
            return '—'
        url = reverse('admin:core_dispute_resolve', args=[obj.pk])
        return format_html('<a class="button" href="{}">حسم النزاع</a>', url)

    def get_urls(self):
        custom = [
            path('<int:pk>/resolve/', self.admin_site.admin_view(self.resolve_view), name='core_dispute_resolve'),
        ]
        return custom + super().get_urls()

    def resolve_view(self, request, pk):
        dispute = get_object_or_404(Dispute, pk=pk)
        if not self.has_change_permission(request, dispute):
            raise PermissionDenied

        reservation = dispute.reservation
        chat = Chat.objects.filter(item=dispute.item).filter(
            Q(buyer=dispute.complainant, seller=dispute.defendant) |
            Q(buyer=dispute.defendant, seller=dispute.complainant)
        ).first()
        chat_messages = chat.messages.select_related('sender').order_by('created_at') if chat else Message.objects.none()

        if request.method == 'POST' and dispute.status not in ('resolved', 'closed'):
            resolution_action = request.POST.get('resolution_action')
            resolution_text = request.POST.get('resolution', '')
            admin_notes = request.POST.get('admin_notes', '')
            partial_raw = request.POST.get('partial_refund_amount')
            try:
                partial_refund_amount = Decimal(partial_raw) if partial_raw else None
            except InvalidOperation:
                messages.error(request, 'مبلغ الاسترداد الجزئي غير صالح')
                partial_refund_amount = None
            else:
                try:
                    resolve_dispute(
                        dispute,
                        resolution_action=resolution_action,
                        resolution=resolution_text,
                        admin_notes=admin_notes,
                        partial_refund_amount=partial_refund_amount,
                    )
                    messages.success(
                        request,
                        f'تم حسم النزاع #{dispute.id}: '
                        f'{dict(Dispute.RESOLUTION_CHOICES).get(resolution_action, resolution_action)}'
                    )
                    return redirect('admin:core_dispute_changelist')
                except DisputeResolutionError as e:
                    messages.error(request, str(e))
                except ValueError as e:
                    messages.error(request, f'تعذّر تنفيذ التحويل المالي لهذا القرار: {e}')

        context = dict(
            self.admin_site.each_context(request),
            title=f'حسم النزاع #{dispute.id}',
            dispute=dispute,
            reservation=reservation,
            chat=chat,
            chat_messages=chat_messages,
            resolution_choices=Dispute.RESOLUTION_CHOICES,
            opts=self.model._meta,
        )
        return TemplateResponse(request, 'admin/core/dispute/resolve.html', context)


_REPORT_BADGE_COLORS = {
    'pending': ('#9C6216', '#9C621620'),
    'reviewing': ('#1D4E89', '#1D4E8920'),
    'resolved': ('#2E7D57', '#2E7D5720'),
    'dismissed': ('#6B7280', '#6B728020'),
}


@admin.register(Report)
class ReportAdmin(ModelAdmin):
    """
    Whoever/whatever a report targets is only ever suspended/archived
    through the target's own admin action (Item's "أرشفة المنتجات
    المحددة", User's "تعليق الحسابات المحددة", UserStore's "تعطيل المتاجر
    المحددة") or the DRF resolve action — never a raw field edit here — so
    a report can't silently drift from what those guarded paths do. The
    workflow: open a report, set action_taken + admin_notes to record the
    decision, then use "حسم البلاغات المحددة" below to close it out — that
    action is what actually calls resolve_report() and applies the
    recorded action_taken to the target.
    """
    list_display = ('id', 'target_display', 'reason', 'reporter', 'status_badge', 'action_taken', 'created_at')
    list_filter = ('status', 'reason', 'action_taken', 'created_at')
    search_fields = (
        'reporter__username', 'description',
        'reported_item__title', 'reported_store__name', 'reported_user__username',
    )
    raw_id_fields = ('reporter', 'reported_item', 'reported_store', 'reported_user', 'reviewed_by')
    list_select_related = ('reporter', 'reported_item', 'reported_store', 'reported_user', 'reviewed_by')
    readonly_fields = (
        'reporter', 'reported_item', 'reported_store', 'reported_user',
        'reason', 'description', 'status', 'reviewed_by', 'created_at', 'resolved_at',
    )
    actions = ['resolve_reports_action', 'dismiss_reports_action', 'mark_reviewing_action']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        # Mirrors ItemAdmin/ReservationAdmin below — a report is the raw
        # record behind a moderation decision; staff shouldn't be able to
        # erase it here when ReportViewSet.destroy blocks the same thing
        # via the API.
        return False

    @admin.display(description='الهدف')
    def target_display(self, obj):
        return str(obj.target)

    @admin.display(description='الحالة')
    def status_badge(self, obj):
        color, bg = _REPORT_BADGE_COLORS.get(obj.status, ('#6B7280', '#6B728020'))
        return format_html(
            '<span style="background:{};color:{};padding:2px 10px;border-radius:999px;'
            'font-size:12px;font-weight:600;white-space:nowrap;">{}</span>',
            bg, color, obj.get_status_display(),
        )

    @admin.action(description='حسم البلاغات المحددة (تنفيذ الإجراء المسجّل)', permissions=['change'])
    def resolve_reports_action(self, request, queryset):
        count, failed = 0, 0
        for report in queryset.filter(status__in=['pending', 'reviewing']):
            try:
                resolve_report(
                    report, new_status='resolved', action_taken=report.action_taken,
                    admin_notes=report.admin_notes, actor=request.user,
                )
                notify_report_reviewed(report)
                count += 1
            except ReportResolutionError as e:
                failed += 1
                self.message_user(request, f'بلاغ #{report.id}: {e}', level=messages.ERROR)
        if count:
            self.message_user(request, f'تم حسم {count} بلاغ.', level=messages.SUCCESS)

    @admin.action(description='رفض البلاغات المحددة (لا إجراء)', permissions=['change'])
    def dismiss_reports_action(self, request, queryset):
        count = 0
        for report in queryset.filter(status__in=['pending', 'reviewing']):
            resolve_report(
                report, new_status='dismissed', action_taken='none',
                admin_notes=report.admin_notes, actor=request.user,
            )
            notify_report_reviewed(report)
            count += 1
        self.message_user(request, f'تم رفض {count} بلاغ.', level=messages.SUCCESS)

    @admin.action(description='وضع علامة "قيد المراجعة" على البلاغات المحددة', permissions=['change'])
    def mark_reviewing_action(self, request, queryset):
        count = queryset.filter(status='pending').update(status='reviewing')
        self.message_user(request, f'تم تحديث {count} بلاغ إلى "قيد المراجعة".', level=messages.SUCCESS)


# =============================================================================
# الثقة والمراسلات
# =============================================================================

class LowScoreFilter(admin.SimpleListFilter):
    """There's no user-facing "report" flag on reviews — a low score is
    the closest signal the data actually has for "this might need a
    moderator's attention", so that's what this surfaces."""
    title = 'مستوى التقييم'
    parameter_name = 'score_level'

    def lookups(self, request, model_admin):
        return [('low', 'منخفض (1-2 نجوم) — يحتاج مراجعة')]

    def queryset(self, request, queryset):
        if self.value() == 'low':
            return queryset.filter(score__lte=2)
        return queryset


@admin.register(Rating)
class RatingAdmin(ModelAdmin):
    list_display = ('rater', 'rated_user', 'rating_type', 'score', 'item', 'created_at')
    list_filter = ('rating_type', 'score', LowScoreFilter)
    search_fields = ('rater__username', 'rated_user__username')
    raw_id_fields = ('rater', 'rated_user', 'item')
    list_select_related = ('rater', 'rated_user', 'item', 'item__owner')


@admin.register(ProductReview)
class ProductReviewAdmin(ModelAdmin):
    list_display = ('reviewer', 'item', 'score', 'created_at')
    list_filter = ('score', LowScoreFilter)
    search_fields = ('reviewer__username', 'item__title')
    raw_id_fields = ('reviewer', 'item')
    list_select_related = ('reviewer', 'item', 'item__owner')


@admin.register(StoreReview)
class StoreReviewAdmin(ModelAdmin):
    list_display = ('reviewer', 'store', 'score', 'created_at')
    list_filter = ('score', LowScoreFilter)
    search_fields = ('reviewer__username', 'store__name')
    raw_id_fields = ('reviewer', 'store')
    list_select_related = ('reviewer', 'store', 'store__user')


class SupportChatFilter(admin.SimpleListFilter):
    title = 'نوع المحادثة'
    parameter_name = 'kind'

    def lookups(self, request, model_admin):
        return [('support', 'محادثات الدعم')]

    def queryset(self, request, queryset):
        if self.value() == 'support':
            return queryset.filter(item__isnull=True, store__isnull=True, seller__username=SUPPORT_USERNAME)
        return queryset


@admin.register(Chat)
class ChatAdmin(ModelAdmin):
    """
    Private buyer/seller conversations — visible to whoever holds view_chat
    (support + superuser, see the permission groups migration), read-only
    so moderating a dispute never doubles as editing evidence.

    The one exception: "التواصل مع الدعم" conversations (seller is the
    fixed support account, see core/services/support.py) get a reply_view
    below — staff answer from here rather than needing a Flutter login of
    their own. Message.sender records the actual staff member; Chat.seller
    stays the fixed support identity either way.
    """
    list_display = ('id', 'item', 'store', 'buyer', 'seller', 'is_active', 'updated_at', 'reply_link')
    list_filter = ('is_active', SupportChatFilter)
    search_fields = ('buyer__username', 'seller__username', 'item__title')
    raw_id_fields = ('item', 'store', 'buyer', 'seller')
    list_select_related = ('item', 'item__owner', 'store', 'store__user', 'buyer', 'seller')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def _is_support_chat(self, obj):
        return obj.item_id is None and obj.store_id is None and obj.seller.username == SUPPORT_USERNAME

    @admin.display(description='')
    def reply_link(self, obj):
        if not self._is_support_chat(obj):
            return '—'
        url = reverse('admin:core_chat_reply', args=[obj.pk])
        return format_html('<a class="button" href="{}">الرد</a>', url)

    def get_urls(self):
        custom = [
            path('<int:pk>/reply/', self.admin_site.admin_view(self.reply_view), name='core_chat_reply'),
        ]
        return custom + super().get_urls()

    def reply_view(self, request, pk):
        chat = get_object_or_404(Chat.objects.select_related('buyer', 'seller'), pk=pk)
        if not self.has_view_permission(request, chat):
            raise PermissionDenied
        if not self._is_support_chat(chat):
            # This view only exists for support conversations — a regular
            # buyer/seller chat stays fully read-only, no exceptions.
            raise PermissionDenied

        if request.method == 'POST':
            content = request.POST.get('content', '').strip()
            if not content:
                messages.error(request, 'لا يمكن إرسال رد فارغ.')
            else:
                message = Message.objects.create(
                    chat=chat, sender=request.user, message_type='user', content=content,
                )
                try:
                    from .consumers import broadcast_chat_message
                    broadcast_chat_message(message)
                except Exception:
                    pass  # best-effort — the reply is saved either way
                messages.success(request, 'تم إرسال الرد.')
                return redirect('admin:core_chat_reply', pk=chat.pk)

        context = dict(
            self.admin_site.each_context(request),
            title=f'محادثة الدعم مع {chat.buyer.username}',
            chat=chat,
            chat_messages=chat.messages.select_related('sender').order_by('created_at'),
            opts=self.model._meta,
        )
        return TemplateResponse(request, 'admin/core/chat/reply.html', context)


@admin.register(Message)
class MessageAdmin(ModelAdmin):
    list_display = ('id', 'chat', 'sender', 'message_type', 'content_preview', 'is_read', 'created_at')
    list_filter = ('message_type', 'is_read')
    search_fields = ('sender__username', 'content')
    raw_id_fields = ('chat', 'sender')
    list_select_related = ('chat', 'chat__buyer', 'chat__seller', 'chat__item', 'chat__store', 'sender')

    @admin.display(description='المحتوى')
    def content_preview(self, obj):
        return obj.content[:60]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# =============================================================================
# التسويق والعروض
# =============================================================================

@admin.register(StoreCoupon)
class StoreCouponAdmin(ModelAdmin):
    list_display = ('code', 'store', 'amount', 'min_order_amount',
                    'is_active', 'expires_at', 'created_at')
    list_filter = ('is_active', 'created_at')
    search_fields = ('code', 'store__name')
    raw_id_fields = ('store',)
    list_select_related = ('store', 'store__user')


@admin.register(CouponCollection)
class CouponCollectionAdmin(ModelAdmin):
    list_display = ('user', 'coupon', 'collected_at')
    search_fields = ('user__username', 'coupon__code')
    raw_id_fields = ('user', 'coupon')
    list_select_related = ('user', 'coupon', 'coupon__store')


# =============================================================================
# البث المباشر
# =============================================================================

class LiveSessionItemInline(TabularInline):
    model = LiveSessionItem
    extra = 0


@admin.register(LiveSession)
class LiveSessionAdmin(ModelAdmin):
    list_display = ('title', 'store', 'status', 'current_viewer_count', 'likes_count', 'started_at')
    list_filter = ('status',)
    search_fields = ('title', 'store__name')
    raw_id_fields = ('store',)
    list_select_related = ('store', 'store__user')
    inlines = [LiveSessionItemInline]
    actions = ['stop_selected_sessions']

    @admin.action(description='إيقاف طارئ للبث المباشر المحدد', permissions=['change'])
    def stop_selected_sessions(self, request, queryset):
        count = 0
        for session in queryset.filter(status='live'):
            stop_live_session(session)
            count += 1
        if count:
            self.message_user(request, f'تم إيقاف {count} جلسة بث مباشر.', level=messages.SUCCESS)
        else:
            self.message_user(request, 'لا توجد جلسات مباشرة فعليًا ضمن التحديد.', level=messages.WARNING)


@admin.register(LiveSessionLike)
class LiveSessionLikeAdmin(ModelAdmin):
    list_display = ('user', 'session', 'created_at')
    search_fields = ('user__username', 'session__title')
    raw_id_fields = ('user', 'session')
    list_select_related = ('user', 'session', 'session__store')


@admin.register(LiveSessionComment)
class LiveSessionCommentAdmin(ModelAdmin):
    list_display = ('user', 'session', 'content', 'created_at')
    search_fields = ('user__username', 'session__title', 'content')
    raw_id_fields = ('user', 'session')
    list_select_related = ('user', 'session', 'session__store')


# =============================================================================
# التوصيل
# =============================================================================

@admin.register(DeliveryCompany)
class DeliveryCompanyAdmin(ModelAdmin):
    list_display = (
        'name', 'account', 'primary_contact_phone',
        'intra_governorate_fee', 'inter_governorate_fee', 'outstanding_liability_badge', 'is_active', 'created_at',
    )
    list_filter = ('is_active',)
    search_fields = ('name', 'account__username', 'primary_contact_phone')
    raw_id_fields = ('account',)
    list_select_related = ('account',)

    @admin.display(description='الذمة النقدية الحالية')
    def outstanding_liability_badge(self, obj):
        amount = obj.outstanding_cash_liability()
        if amount <= 0:
            return '—'
        return format_html(
            '<span style="background:#9C621620;color:#9C6216;padding:2px 10px;border-radius:999px;'
            'font-size:12px;font-weight:600;white-space:nowrap;">{}</span>',
            amount,
        )


@admin.register(DeliveryBatch)
class DeliveryBatchAdmin(ModelAdmin):
    """Created only via ReservationViewSet.send_to_delivery_batch (see
    delivery_utils.combine_deliveries) — a manual add here wouldn't have
    any real delivery orders to tag as one trip."""
    list_display = ('id', 'buyer', 'delivery_location', 'company', 'combined_fee', 'order_count', 'created_at')
    list_filter = ('company',)
    search_fields = ('buyer__username', 'company__name')
    raw_id_fields = ('buyer', 'delivery_location', 'company')
    list_select_related = ('buyer', 'delivery_location', 'company')

    @admin.display(description='عدد الطلبات')
    def order_count(self, obj):
        return obj.delivery_orders.count()

    def has_add_permission(self, request):
        return False


@admin.register(DeliveryOrder)
class DeliveryOrderAdmin(ModelAdmin):
    """
    cash_settlement is only ever set through settle_cash_action below — a
    plain field edit could silently untrack a company's cash liability
    without the audit trail CashSettlement exists for.
    """
    list_display = (
        'id', 'reservation', 'direction', 'status', 'company',
        'to_name', 'cash_to_collect', 'cash_collected', 'cash_settlement',
        'delivery_fee', 'assigned_at',
    )
    list_filter = ('direction', 'status', 'company', 'cash_collected')
    search_fields = ('reservation__id', 'item_summary', 'to_name', 'from_name')
    raw_id_fields = ('reservation', 'company', 'dispute')
    list_select_related = ('reservation', 'reservation__item', 'reservation__buyer', 'company', 'cash_settlement')
    readonly_fields = ('cash_settlement',)
    actions = ['settle_cash_action']

    @admin.action(description='تسجيل تسوية نقدية للمبالغ المُحصَّلة', permissions=['change'])
    def settle_cash_action(self, request, queryset):
        eligible = queryset.filter(cash_collected=True, cash_settlement__isnull=True, cash_to_collect__gt=0)

        if not eligible.exists():
            self.message_user(
                request,
                'لا توجد طلبات مؤهَّلة ضمن التحديد — يجب أن تكون محصَّلة نقداً ولم تُسوَّ بعد.',
                level=messages.ERROR,
            )
            return None

        # .order_by() clears DeliveryOrder's default Meta.ordering (by
        # assigned_at) first — left in place, MySQL folds it into the
        # DISTINCT clause and company_id stops deduplicating.
        company_ids = list(eligible.order_by().values_list('company_id', flat=True).distinct())
        if len(company_ids) > 1 or company_ids[0] is None:
            self.message_user(
                request,
                'التحديد يشمل أكثر من شركة توصيل (أو طلباً بلا شركة) — اختر طلبات شركة واحدة فقط لكل تسوية.',
                level=messages.ERROR,
            )
            return None

        company = DeliveryCompany.objects.get(pk=company_ids[0])
        total = eligible.aggregate(total=Sum('cash_to_collect'))['total'] or Decimal('0.00')

        if 'apply' in request.POST:
            notes = request.POST.get('notes', '').strip()
            settlement = CashSettlement.objects.create(
                company=company, amount=total, settled_by=request.user, notes=notes,
            )
            count = eligible.update(cash_settlement=settlement)
            self.message_user(
                request,
                f'تم تسجيل تسوية بمبلغ {total} مع {company.name} ({count} طلب).',
                level=messages.SUCCESS,
            )
            return None

        context = dict(
            self.admin_site.each_context(request),
            title='تسجيل تسوية نقدية',
            queryset=eligible,
            company=company,
            total=total,
            opts=self.model._meta,
            action_checkbox_name=ACTION_CHECKBOX_NAME,
        )
        return TemplateResponse(request, 'admin/core/deliveryorder/settle_cash_confirmation.html', context)


@admin.register(CashSettlement)
class CashSettlementAdmin(ModelAdmin):
    """Created only via DeliveryOrderAdmin.settle_cash_action — a manual add
    here would have no delivery orders backing it, and editing the amount
    after the fact would desync it from what was actually recorded."""
    list_display = ('company', 'amount', 'settled_by', 'order_count', 'created_at')
    list_filter = ('company',)
    search_fields = ('company__name', 'notes')
    raw_id_fields = ('company', 'settled_by')
    list_select_related = ('company', 'settled_by')
    readonly_fields = ('company', 'amount', 'settled_by', 'notes', 'created_at')

    @admin.display(description='عدد الطلبات')
    def order_count(self, obj):
        return obj.delivery_orders.count()

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# =============================================================================
# المال والمحافظ
# =============================================================================

class WalletAnomalyFilter(admin.SimpleListFilter):
    """Neither should ever happen given Wallet.debit()/hold() check
    available_balance first — this exists to *notice* if one ever does
    (a bug, a race condition, direct DB surgery), not to explain it away."""
    title = 'حالة الرصيد'
    parameter_name = 'anomaly'

    def lookups(self, request, model_admin):
        return [
            ('negative', 'رصيد سالب'),
            ('overheld', 'محجوز يتجاوز الرصيد'),
        ]

    def queryset(self, request, queryset):
        if self.value() == 'negative':
            return queryset.filter(balance__lt=0)
        if self.value() == 'overheld':
            return queryset.filter(held_balance__gt=F('balance'))
        return queryset


@admin.register(Wallet)
class WalletAdmin(ModelAdmin):
    """balance/held_balance move only through Wallet.credit/debit/hold
    (row-locked F() updates paired with a Transaction row) — editing them
    as plain admin fields would desync the ledger, so this stays read-only.
    Correcting a wallet means creating a Transaction, not editing this."""
    list_display = ('user', 'balance', 'held_balance', 'available_balance', 'anomaly_badge', 'updated_at')
    list_filter = (WalletAnomalyFilter,)
    search_fields = ('user__username',)
    raw_id_fields = ('user',)
    list_select_related = ('user',)
    readonly_fields = ('user', 'balance', 'held_balance', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='الرصيد المتاح')
    def available_balance(self, obj):
        return obj.available_balance

    @admin.display(description='')
    def anomaly_badge(self, obj):
        if obj.balance < 0:
            return format_html(
                '<span style="background:#AC3A3120;color:#AC3A31;padding:2px 10px;'
                'border-radius:999px;font-size:12px;font-weight:600;">⚠ رصيد سالب</span>'
            )
        if obj.held_balance > obj.balance:
            return format_html(
                '<span style="background:#9C621620;color:#9C6216;padding:2px 10px;'
                'border-radius:999px;font-size:12px;font-weight:600;">⚠ محجوز يتجاوز الرصيد</span>'
            )
        return ''


@admin.register(Notification)
class NotificationAdmin(ModelAdmin):
    list_display = ('user', 'notification_type', 'title', 'is_read', 'created_at')
    list_filter = ('notification_type', 'is_read')
    search_fields = ('user__username', 'title')
    raw_id_fields = ('user',)
    list_select_related = ('user',)
    date_hierarchy = 'created_at'


@admin.register(DepositRequest)
class DepositRequestAdmin(ModelAdmin):
    """WalletViewSet.deposit is a simulated card gateway that completes
    synchronously — there's no pending state and nothing for an admin to
    approve. status/completed_at stay read-only so a deposit can't be
    hand-marked 'completed' independently of that one write path."""
    list_display = ('user', 'amount', 'status', 'card_last4', 'created_at', 'completed_at')
    list_filter = ('status',)
    search_fields = ('user__username', 'reference')
    raw_id_fields = ('user',)
    list_select_related = ('user',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


_WITHDRAWAL_BADGE_COLORS = {
    'pending': ('#9C6216', '#9C621620'),
    'approved': ('#2E7D57', '#2E7D5720'),
    'rejected': ('#6B7280', '#6B728020'),
}


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(ModelAdmin):
    """
    Approving/rejecting must go through review_view below, which calls the
    same approve_withdrawal()/reject_withdrawal() service the DRF endpoint
    uses — status stays read-only here so a plain field edit can't approve
    a payout without the wallet's held_balance actually settling.
    admin_notes is safe to edit directly.
    """
    list_display = ('user', 'amount', 'status_badge', 'card_last4', 'reviewed_by', 'created_at', 'review_link')
    list_filter = ('status',)
    search_fields = ('user__username',)
    raw_id_fields = ('user', 'reviewed_by')
    list_select_related = ('user', 'reviewed_by')
    readonly_fields = ('user', 'amount', 'card_last4', 'status', 'reviewed_by', 'reviewed_at', 'created_at')

    def has_add_permission(self, request):
        return False

    @admin.display(description='الحالة')
    def status_badge(self, obj):
        color, bg = _WITHDRAWAL_BADGE_COLORS.get(obj.status, ('#6B7280', '#6B728020'))
        return format_html(
            '<span style="background:{};color:{};padding:2px 10px;border-radius:999px;'
            'font-size:12px;font-weight:600;white-space:nowrap;">{}</span>',
            bg, color, obj.get_status_display(),
        )

    @admin.display(description='')
    def review_link(self, obj):
        if obj.status != 'pending':
            return '—'
        url = reverse('admin:core_withdrawalrequest_review', args=[obj.pk])
        return format_html('<a class="button" href="{}">مراجعة</a>', url)

    def get_urls(self):
        custom = [
            path('<int:pk>/review/', self.admin_site.admin_view(self.review_view), name='core_withdrawalrequest_review'),
        ]
        return custom + super().get_urls()

    def review_view(self, request, pk):
        withdrawal = get_object_or_404(WithdrawalRequest, pk=pk)
        if not self.has_change_permission(request, withdrawal):
            raise PermissionDenied

        wallet = Wallet.objects.filter(user=withdrawal.user).first()

        if request.method == 'POST' and withdrawal.status == 'pending':
            decision = request.POST.get('decision')
            admin_notes = request.POST.get('admin_notes', '').strip()

            if decision == 'reject' and not admin_notes:
                messages.error(request, 'سبب الرفض إلزامي.')
            elif decision not in ('approve', 'reject'):
                messages.error(request, 'إجراء غير معروف.')
            else:
                try:
                    if decision == 'approve':
                        approve_withdrawal(withdrawal, reviewed_by=request.user, admin_notes=admin_notes)
                        messages.success(request, f'تم اعتماد طلب السحب #{withdrawal.id} وصرف المبلغ.')
                    else:
                        reject_withdrawal(withdrawal, reviewed_by=request.user, admin_notes=admin_notes)
                        messages.success(request, f'تم رفض طلب السحب #{withdrawal.id} وإعادة المبلغ إلى الرصيد المتاح.')
                    return redirect('admin:core_withdrawalrequest_changelist')
                except WithdrawalReviewError as e:
                    messages.error(request, str(e))

        context = dict(
            self.admin_site.each_context(request),
            title=f'مراجعة طلب السحب #{withdrawal.id}',
            withdrawal=withdrawal,
            wallet=wallet,
            opts=self.model._meta,
        )
        return TemplateResponse(request, 'admin/core/withdrawalrequest/review.html', context)


# =============================================================================
# سجل التدقيق (Phase 7)
# =============================================================================
# django-auditlog already registers a filterable LogEntryAdmin (Phase 0) —
# this re-registers it with the unfold theme and adds an actor filter, so
# "who changed what" is answerable without leaving the admin's visual
# language. The underlying LogEntryAdminMixin (has_add/change/delete
# permission overrides, msg/resource_url helpers) is unchanged.
from auditlog.admin import LogEntryAdmin as BaseLogEntryAdmin
from auditlog.models import LogEntry

admin.site.unregister(LogEntry)


@admin.register(LogEntry)
class LogEntryAdmin(BaseLogEntryAdmin, ModelAdmin):
    list_filter = BaseLogEntryAdmin.list_filter + ['actor']


# =============================================================================
# النظام القانوني — الوثائق والموافقات
# =============================================================================
from django import forms
from .models import LegalDocument, LegalAcceptance


@admin.register(LegalDocument)
class LegalDocumentAdmin(ModelAdmin):
    list_display = ('title', 'doc_type', 'language', 'version',
                    'is_published', 'requires_reacceptance', 'effective_at')
    list_display_links = ('title',)
    list_filter = ('doc_type', 'language', 'is_published', 'requires_reacceptance')
    list_editable = ('is_published',)
    search_fields = ('title', 'summary', 'content')
    ordering = ('doc_type', 'language', '-version')
    readonly_fields = ('created_at', 'updated_at')
    fieldsets = (
        (None, {
            'fields': ('doc_type', 'language', 'version', 'title', 'summary'),
        }),
        ('المحتوى', {
            'fields': ('content',),
            'description': 'محتوى HTML — يُعرض كما هو في التطبيق والصفحة العلنية.',
        }),
        ('النشر', {
            'fields': ('is_published', 'requires_reacceptance', 'effective_at',
                       'created_at', 'updated_at'),
        }),
    )
    formfield_overrides = {
        # محرّر أعرض للمحتوى (HTML) بدل الصندوق الصغير الافتراضي.
        LegalDocument._meta.get_field('content').__class__: {
            'widget': forms.Textarea(attrs={'rows': 26, 'style': 'font-family:monospace;width:100%;'}),
        },
    }


@admin.register(LegalAcceptance)
class LegalAcceptanceAdmin(ModelAdmin):
    """سجلّ موافقات المستخدمين — للقراءة فقط (دليل إثبات لا يُعدّل يدوياً)."""
    list_display = ('user', 'document', 'accepted_at', 'ip_address', 'app_version')
    list_filter = ('document__doc_type', 'document__language', 'document__version')
    search_fields = ('user__username', 'user__email', 'ip_address', 'app_version')
    date_hierarchy = 'accepted_at'
    readonly_fields = ('user', 'document', 'accepted_at', 'ip_address',
                       'app_version', 'user_agent')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
