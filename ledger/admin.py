from django.contrib import admin
from unfold.admin import ModelAdmin

from .models import Contact, LedgerEntry


@admin.register(Contact)
class ContactAdmin(ModelAdmin):
    list_display = ('name', 'kind', 'phone', 'store', 'is_archived', 'created_at')
    list_filter = ('kind', 'is_archived')
    search_fields = ('name', 'phone', 'store__name')
    raw_id_fields = ('store', 'linked_user')
    list_select_related = ('store',)


@admin.register(LedgerEntry)
class LedgerEntryAdmin(ModelAdmin):
    """Support can read the book; the merchant writes it. Voiding is the
    only correction the model allows, and that happens through the app."""
    list_display = ('occurred_at', 'store', 'kind', 'amount', 'contact', 'source', 'is_void')
    list_filter = ('kind', 'source', 'is_void', 'expense_category')
    search_fields = ('note', 'store__name', 'contact__name')
    raw_id_fields = ('store', 'contact', 'item', 'variant', 'reservation', 'created_by')
    list_select_related = ('store', 'contact')
    date_hierarchy = 'occurred_at'
    readonly_fields = ('cost_amount', 'source', 'reservation', 'is_void', 'void_reason',
                       'voided_at', 'created_by', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False
