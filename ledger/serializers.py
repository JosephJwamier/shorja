from decimal import Decimal

from rest_framework import serializers

from core.models import Item, ItemVariant
from .models import Contact, LedgerEntry


class ContactSerializer(serializers.ModelSerializer):
    balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    kind_display = serializers.CharField(source='get_kind_display', read_only=True)
    linked_user_username = serializers.CharField(source='linked_user.username', read_only=True, default=None)

    class Meta:
        model = Contact
        fields = ['id', 'kind', 'kind_display', 'name', 'phone', 'notes', 'linked_user',
                  'linked_user_username', 'is_archived', 'balance', 'created_at', 'updated_at']
        read_only_fields = ['id', 'is_archived', 'balance', 'created_at', 'updated_at']

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('الاسم مطلوب')
        return value

    def validate_phone(self, value):
        return (value or '').strip()

    def validate(self, attrs):
        # Same person twice is the classic ledger mess — block exact
        # duplicates (name + phone) within a store.
        store = self.context['store']
        name = attrs.get('name', getattr(self.instance, 'name', ''))
        phone = attrs.get('phone', getattr(self.instance, 'phone', ''))
        dup = Contact.objects.filter(store=store, name__iexact=name, phone=phone, is_archived=False)
        if self.instance:
            dup = dup.exclude(pk=self.instance.pk)
        if dup.exists():
            raise serializers.ValidationError({'name': 'هذا الطرف مسجّل مسبقاً'})
        return attrs


class LedgerEntrySerializer(serializers.ModelSerializer):
    kind_display = serializers.CharField(source='get_kind_display', read_only=True)
    contact_name = serializers.CharField(source='contact.name', read_only=True, default=None)
    item_title = serializers.CharField(source='item.title', read_only=True, default=None)
    variant_label = serializers.SerializerMethodField()
    receipt_url = serializers.SerializerMethodField()
    expense_category_display = serializers.CharField(
        source='get_expense_category_display', read_only=True)

    class Meta:
        model = LedgerEntry
        fields = ['id', 'kind', 'kind_display', 'amount', 'contact', 'contact_name', 'note',
                  'occurred_at', 'expense_category', 'expense_category_display',
                  'item', 'item_title', 'variant', 'variant_label', 'quantity', 'cost_amount',
                  'receipt', 'receipt_url', 'source', 'reservation',
                  'is_void', 'void_reason', 'voided_at', 'created_at']
        read_only_fields = ['id', 'cost_amount', 'source', 'reservation',
                            'is_void', 'void_reason', 'voided_at', 'created_at']
        extra_kwargs = {'receipt': {'write_only': True, 'required': False}}

    def get_variant_label(self, obj):
        return obj.variant.label if obj.variant_id else None

    def get_receipt_url(self, obj):
        if not obj.receipt:
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.receipt.url) if request else obj.receipt.url

    # -- validation -------------------------------------------------------

    def validate_kind(self, value):
        if value not in LedgerEntry.MANUAL_KINDS:
            raise serializers.ValidationError('هذا النوع يُسجَّل تلقائياً ولا يُدخل يدوياً')
        return value

    def validate_amount(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError('المبلغ يجب أن يكون أكبر من صفر')
        return value

    def validate(self, attrs):
        store = self.context['store']
        # On update only the descriptive fields may change; money, kind and
        # stock are frozen (void + re-enter instead, so the trail is honest).
        if self.instance is not None:
            frozen = {'kind', 'amount', 'contact', 'item', 'variant', 'quantity'}
            changed = {k for k in frozen if k in attrs and attrs[k] != getattr(self.instance, k)}
            if changed:
                raise serializers.ValidationError(
                    'لا يمكن تعديل المبلغ أو النوع أو الطرف بعد التسجيل — ألغِ القيد وسجّل غيره')
            return attrs

        kind = attrs['kind']
        contact = attrs.get('contact')
        wanted_kind = LedgerEntry.CONTACT_KIND_FOR.get(kind)
        if wanted_kind and contact is None:
            raise serializers.ValidationError({'contact': 'اختر الطرف أولاً'})
        if contact is not None:
            if contact.store_id != store.id:
                raise serializers.ValidationError({'contact': 'الطرف ليس في دفترك'})
            if wanted_kind and contact.kind != wanted_kind:
                label = dict(Contact.KIND_CHOICES)[wanted_kind]
                raise serializers.ValidationError({'contact': f'هذا القيد يحتاج {label}'})
            if not wanted_kind and kind != LedgerEntry.KIND_SALE_CASH:
                # A contact on an expense makes no sense; a cash sale may
                # name the customer for the record.
                raise serializers.ValidationError({'contact': 'هذا النوع لا يرتبط بطرف'})

        if kind == LedgerEntry.KIND_EXPENSE and not attrs.get('expense_category'):
            attrs['expense_category'] = 'other'
        if kind != LedgerEntry.KIND_EXPENSE:
            attrs['expense_category'] = ''

        item = attrs.get('item')
        variant = attrs.get('variant')
        quantity = attrs.get('quantity')
        if item is not None or variant is not None or quantity:
            if kind not in (LedgerEntry.KIND_SALE_CASH, LedgerEntry.KIND_SALE_CREDIT):
                raise serializers.ValidationError({'item': 'الربط بالمخزون للمبيعات فقط'})
            if item is None or item.owner_id != store.user_id:
                raise serializers.ValidationError({'item': 'المنتج ليس من منتجاتك'})
            if variant is None:
                variant = item.default_variant
                attrs['variant'] = variant
            if variant is None or variant.item_id != item.id or not variant.is_active:
                raise serializers.ValidationError({'variant': 'اختر المتغيّر الصحيح'})
            if not quantity or quantity < 1:
                raise serializers.ValidationError({'quantity': 'أدخل الكمية'})
            if quantity > variant.quantity:
                raise serializers.ValidationError(
                    {'quantity': f'المتوفّر من هذا المتغيّر {variant.quantity} فقط'})
        return attrs


class VoidSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default='')


class StatementRowSerializer(serializers.Serializer):
    """One line of a contact's statement with the running balance."""
    entry = LedgerEntrySerializer()
    running_balance = serializers.DecimalField(max_digits=14, decimal_places=2)


# ---- read-only shapes for summary/dashboard ------------------------------

class DailyPointSerializer(serializers.Serializer):
    day = serializers.DateField()
    local = serializers.DecimalField(max_digits=14, decimal_places=2)
    app = serializers.DecimalField(max_digits=14, decimal_places=2)
    out = serializers.DecimalField(max_digits=14, decimal_places=2)


class SummarySerializer(serializers.Serializer):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    revenue = serializers.DecimalField(max_digits=14, decimal_places=2)
    sales_cash = serializers.DecimalField(max_digits=14, decimal_places=2)
    sales_credit = serializers.DecimalField(max_digits=14, decimal_places=2)
    sales_app = serializers.DecimalField(max_digits=14, decimal_places=2)
    sales_count = serializers.IntegerField()
    payments_in = serializers.DecimalField(max_digits=14, decimal_places=2)
    expenses = serializers.DecimalField(max_digits=14, decimal_places=2)
    purchases = serializers.DecimalField(max_digits=14, decimal_places=2)
    payments_out = serializers.DecimalField(max_digits=14, decimal_places=2)
    net = serializers.DecimalField(max_digits=14, decimal_places=2)
    cash_in = serializers.DecimalField(max_digits=14, decimal_places=2)
    cash_out = serializers.DecimalField(max_digits=14, decimal_places=2)
    cost_of_sales = serializers.DecimalField(max_digits=14, decimal_places=2)
    costed_revenue = serializers.DecimalField(max_digits=14, decimal_places=2)
    gross_profit = serializers.DecimalField(max_digits=14, decimal_places=2, allow_null=True)
    expenses_by_category = serializers.ListField(child=serializers.DictField())
    daily = DailyPointSerializer(many=True)


class LowStockSerializer(serializers.ModelSerializer):
    item_id = serializers.IntegerField(source='item.id')
    item_title = serializers.CharField(source='item.title')
    label = serializers.CharField()

    class Meta:
        model = ItemVariant
        fields = ['id', 'item_id', 'item_title', 'label', 'quantity']


class DashboardSerializer(serializers.Serializer):
    period = serializers.DictField()
    summary = SummarySerializer()
    previous = serializers.DictField()
    balances = serializers.SerializerMethodField()
    stock = serializers.SerializerMethodField()
    orders = serializers.DictField()
    followers = serializers.IntegerField()
    rating = serializers.DecimalField(max_digits=3, decimal_places=2)
    top_items = serializers.ListField(child=serializers.DictField())

    def get_balances(self, obj):
        b = obj['balances']
        return {
            'receivables': str(b['receivables']),
            'payables': str(b['payables']),
            'debtors_count': b['debtors_count'],
            'top_debtors': ContactSerializer(b['top_debtors'], many=True).data,
            'top_creditors': ContactSerializer(b['top_creditors'], many=True).data,
        }

    def get_stock(self, obj):
        st = obj['stock']
        return {
            'active_items': st['active_items'],
            'sold_out_items': st['sold_out_items'],
            'low_stock': LowStockSerializer(st['low_stock'], many=True).data,
        }
