"""The merchant's book (دفتر الحساب).

Every sale made through the app lands here automatically, and everything
else the merchant does — a cash sale in the shop, a sale on credit, a
customer paying off, a bill — is written by hand. One book, two sources,
so the dashboard can read the whole business rather than just the slice
that passed through the app.

Design rules that the rest of the code relies on:

* Nothing is ever deleted. An entry that was wrong is *voided* with a
  reason; the row stays so a customer's statement and the audit trail
  still add up. Contacts with entries are archived, not removed.
* An entry's effect on a contact's balance is fixed by its `kind`
  (see `Contact.compute_balance`); amounts are always positive.
* Stock is the one thing a ledger row is allowed to change outside this
  app, and only through `ItemVariant.take_stock` / `return_stock` — the
  same choke point the in-app reservation flow uses.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Case, DecimalField, F, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone


class Contact(models.Model):
    """A customer the merchant sells to on credit, or a supplier they buy
    from on credit. Both are the same shape; `kind` decides which way the
    balance reads."""

    KIND_CUSTOMER = 'customer'
    KIND_SUPPLIER = 'supplier'
    KIND_CHOICES = [
        (KIND_CUSTOMER, 'زبون'),
        (KIND_SUPPLIER, 'مورّد'),
    ]

    store = models.ForeignKey(
        'core.UserStore', on_delete=models.CASCADE, related_name='ledger_contacts',
        verbose_name='المتجر',
    )
    kind = models.CharField('النوع', max_length=10, choices=KIND_CHOICES, default=KIND_CUSTOMER)
    name = models.CharField('الاسم', max_length=120)
    phone = models.CharField('الهاتف', max_length=20, blank=True)
    notes = models.TextField('ملاحظات', blank=True)
    # When the customer is also an app user (matched by phone or picked by
    # the merchant), their in-app purchases can show on the same statement.
    linked_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='حساب مرتبط',
    )
    is_archived = models.BooleanField('مؤرشف', default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'طرف في الدفتر'
        verbose_name_plural = 'أطراف الدفتر'
        ordering = ['name']
        indexes = [models.Index(fields=['store', 'kind', 'is_archived'])]

    def __str__(self):
        return f'{self.name} ({self.get_kind_display()})'

    # Which entry kinds move a contact's balance, and in which direction.
    # Positive balance = "they owe me" for a customer, "I owe them" for a
    # supplier — i.e. positive is always the open amount on that contact.
    BALANCE_UP = ('sale_credit', 'purchase')
    BALANCE_DOWN = ('payment_in', 'payment_out')

    @classmethod
    def annotate_balance(cls, queryset):
        """Adds `balance` in one query so lists can sort by it."""
        signed = Case(
            When(entries__kind__in=cls.BALANCE_UP, entries__is_void=False, then=F('entries__amount')),
            When(entries__kind__in=cls.BALANCE_DOWN, entries__is_void=False, then=-F('entries__amount')),
            default=Value(Decimal('0')),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        )
        return queryset.annotate(balance=Coalesce(
            Sum(signed), Value(Decimal('0')),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ))

    def compute_balance(self):
        return type(self).annotate_balance(
            type(self).objects.filter(pk=self.pk)
        ).values_list('balance', flat=True).first() or Decimal('0.00')


class LedgerEntry(models.Model):
    # Money in
    KIND_SALE_CASH = 'sale_cash'        # sold in the shop, paid now
    KIND_SALE_CREDIT = 'sale_credit'    # sold in the shop, customer owes
    KIND_SALE_APP = 'sale_app'          # sold through the app (automatic)
    KIND_PAYMENT_IN = 'payment_in'      # customer paid off (part of) a debt
    # Money out
    KIND_EXPENSE = 'expense'            # rent, power, salaries, cash purchases…
    KIND_PURCHASE = 'purchase'          # bought goods on credit from a supplier
    KIND_PAYMENT_OUT = 'payment_out'    # paid a supplier
    KIND_CHOICES = [
        (KIND_SALE_CASH, 'بيعة نقدية'),
        (KIND_SALE_CREDIT, 'بيع بالدين'),
        (KIND_SALE_APP, 'بيع عبر التطبيق'),
        (KIND_PAYMENT_IN, 'تسديد من زبون'),
        (KIND_EXPENSE, 'مصروف'),
        (KIND_PURCHASE, 'شراء بالدين'),
        (KIND_PAYMENT_OUT, 'تسديد لمورّد'),
    ]
    SALE_KINDS = (KIND_SALE_CASH, KIND_SALE_CREDIT, KIND_SALE_APP)
    # Kinds the merchant may write by hand; sale_app is only ever automatic.
    MANUAL_KINDS = (KIND_SALE_CASH, KIND_SALE_CREDIT, KIND_PAYMENT_IN,
                    KIND_EXPENSE, KIND_PURCHASE, KIND_PAYMENT_OUT)
    # Which contact kind each kind pairs with (None = no contact).
    CONTACT_KIND_FOR = {
        KIND_SALE_CREDIT: Contact.KIND_CUSTOMER,
        KIND_PAYMENT_IN: Contact.KIND_CUSTOMER,
        KIND_PURCHASE: Contact.KIND_SUPPLIER,
        KIND_PAYMENT_OUT: Contact.KIND_SUPPLIER,
    }

    EXPENSE_CATEGORY_CHOICES = [
        ('rent', 'إيجار'),
        ('power', 'كهرباء ومولّدة'),
        ('salaries', 'رواتب'),
        ('transport', 'نقل وتوصيل'),
        ('goods', 'شراء بضاعة'),
        ('commission', 'عمولة المنصّة'),
        ('other', 'أخرى'),
    ]

    SOURCE_MANUAL = 'manual'
    SOURCE_APP = 'app'
    SOURCE_CHOICES = [(SOURCE_MANUAL, 'يدوي'), (SOURCE_APP, 'التطبيق')]

    store = models.ForeignKey(
        'core.UserStore', on_delete=models.CASCADE, related_name='ledger_entries',
        verbose_name='المتجر',
    )
    kind = models.CharField('النوع', max_length=15, choices=KIND_CHOICES)
    amount = models.DecimalField(
        'المبلغ', max_digits=12, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
    )
    # PROTECT: a contact with history is archived, never deleted.
    contact = models.ForeignKey(
        Contact, on_delete=models.PROTECT, null=True, blank=True,
        related_name='entries', verbose_name='الطرف',
    )
    note = models.CharField('ملاحظة', max_length=255, blank=True)
    # When it happened, as the merchant says — they often write up the
    # day's sales in the evening. `created_at` keeps when it was written.
    occurred_at = models.DateTimeField('التاريخ', default=timezone.now, db_index=True)
    expense_category = models.CharField(
        'تصنيف المصروف', max_length=15, choices=EXPENSE_CATEGORY_CHOICES, blank=True,
    )

    # Optional link to stock: "I sold 2 of the blue/M from the shelf".
    item = models.ForeignKey(
        'core.Item', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_entries', verbose_name='المنتج',
    )
    variant = models.ForeignKey(
        'core.ItemVariant', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_entries', verbose_name='المتغيّر',
    )
    quantity = models.PositiveIntegerField('الكمية', null=True, blank=True)
    # Cost of the goods in this sale, frozen at write time from the
    # variant's cost price so a later cost change doesn't rewrite history.
    cost_amount = models.DecimalField(
        'الكلفة', max_digits=12, decimal_places=2, null=True, blank=True,
    )
    receipt = models.ImageField('صورة الوصل', upload_to='ledger_receipts/', null=True, blank=True)

    source = models.CharField('المصدر', max_length=10, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    reservation = models.OneToOneField(
        'core.Reservation', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ledger_entry', verbose_name='الحجز',
    )

    is_void = models.BooleanField('ملغى', default=False)
    void_reason = models.CharField('سبب الإلغاء', max_length=255, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='سجّله',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'قيد في الدفتر'
        verbose_name_plural = 'قيود الدفتر'
        ordering = ['-occurred_at', '-id']
        indexes = [
            models.Index(fields=['store', 'occurred_at']),
            models.Index(fields=['store', 'kind', 'is_void']),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} {self.amount} — {self.store_id}'

    @property
    def is_sale(self):
        return self.kind in self.SALE_KINDS

    @property
    def moves_stock(self):
        return self.is_sale and self.variant_id is not None and (self.quantity or 0) > 0

    def void(self, reason, actor=None):
        """Retire the entry without losing it. Returns stock the entry took."""
        if self.is_void:
            return
        self.is_void = True
        self.void_reason = reason or ''
        self.voided_at = timezone.now()
        self.save(update_fields=['is_void', 'void_reason', 'voided_at', 'updated_at'])
        # App sales hand stock back through the reservation flow itself
        # (cancel/refund already call return_stock); only manual entries
        # own their stock movement.
        if self.source == self.SOURCE_MANUAL and self.moves_stock:
            from core.models import ItemVariant
            variant = ItemVariant.objects.select_for_update().filter(pk=self.variant_id).first()
            if variant is not None:
                variant.return_stock(self.quantity)
