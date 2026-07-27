"""
Phase 2 read-only guardrails.

A handful of ModelAdmins deliberately block add/change in the admin:
Wallet (balance only moves through Wallet.credit/debit/hold, which pairs
each change with a Transaction row — a raw field edit would desync the
ledger), DepositRequest/WithdrawalRequest (their status transitions have
side effects — settling held_balance, confirming money actually arrived —
that a plain field edit skips), Chat/Message (private conversations; admins
read them for support, not edit them), and ItemLocationHistory (an
auto-generated log). These tests exist so a future edit that flips one of
those has_*_permission overrides back to the Django default gets caught
here instead of quietly reopening a way to corrupt a wallet.
"""
import pytest
from django.contrib import admin

from core.models import User, Wallet, DepositRequest, WithdrawalRequest, Chat, ItemLocationHistory

pytestmark = pytest.mark.django_db


def _admin_for(model):
    return admin.site._registry[model]


def _superuser():
    return User.objects.create_superuser(username='owner', password='x', email='o@example.com')


def test_wallet_is_fully_read_only_even_for_superuser():
    model_admin = _admin_for(Wallet)
    request = type('R', (), {'user': _superuser()})()
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False


def test_deposit_request_status_is_not_hand_editable():
    model_admin = _admin_for(DepositRequest)
    request = type('R', (), {'user': _superuser()})()
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False


def test_withdrawal_request_status_field_is_readonly_but_notes_are_not():
    model_admin = _admin_for(WithdrawalRequest)
    request = type('R', (), {'user': _superuser()})()
    assert model_admin.has_add_permission(request) is False
    readonly = model_admin.readonly_fields
    assert 'status' in readonly
    assert 'admin_notes' not in readonly


def test_chat_is_read_only():
    model_admin = _admin_for(Chat)
    request = type('R', (), {'user': _superuser()})()
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False


def test_item_location_history_is_read_only():
    model_admin = _admin_for(ItemLocationHistory)
    request = type('R', (), {'user': _superuser()})()
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False


def test_bid_inline_on_auction_listing_cannot_be_added_by_hand():
    from core.models import AuctionListing
    from core.admin import BidInline

    inline = BidInline(AuctionListing, admin.site)
    request = type('R', (), {'user': _superuser()})()
    assert inline.has_add_permission(request, None) is False
