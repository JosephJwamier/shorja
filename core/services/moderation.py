"""
Content and account moderation actions (Phase 6).

Suspension/archiving reasons are appended to the target's own admin_notes
(timestamped, attributed) rather than overwritten, so a history of past
moderation actions stays readable right next to the record — complementing
django-auditlog's raw field-diff trail (Phase 0), which shows *that*
is_active/status changed but not *why* in a moderator's own words.

is_active=False takes effect immediately, not just on the next login:
SimpleJWT's JWTAuthentication.get_user() checks it on every authenticated
request and rejects an existing access token once it flips.
"""
from django.db import transaction
from django.utils import timezone

from ..live_utils import end_live_session


def _append_note(existing, actor, action_label, reason=''):
    timestamp = timezone.now().strftime('%Y-%m-%d %H:%M')
    who = actor.username if actor else 'system'
    line = f'[{timestamp} — {who}] {action_label}'
    if reason:
        line += f': {reason}'
    existing = (existing or '').strip()
    return f'{existing}\n{line}'.strip()


def suspend_user(user, *, reason, actor):
    """Suspends login access (is_active=False) without touching the
    username/email — unlike the self-service account-deletion flow, this
    is meant to be reversible and the account should stay recognizable."""
    if not reason or not reason.strip():
        raise ValueError('Suspending a user requires a reason')

    user.is_active = False
    user.admin_notes = _append_note(user.admin_notes, actor, 'تعليق الحساب', reason)
    user.save(update_fields=['is_active', 'admin_notes'])
    return user


def reactivate_user(user, *, actor):
    user.is_active = True
    user.admin_notes = _append_note(user.admin_notes, actor, 'إعادة تفعيل الحساب')
    user.save(update_fields=['is_active', 'admin_notes'])
    return user


def archive_item(item, *, reason, actor):
    """Pulls a listing out of circulation (status='archived') — the same
    status a seller reaches by archiving their own item, so nothing else
    needs to special-case an admin-archived item."""
    if not reason or not reason.strip():
        raise ValueError('Archiving an item requires a reason')

    item.status = 'archived'
    item.admin_notes = _append_note(item.admin_notes, actor, 'أرشفة المنتج', reason)
    item.save(update_fields=['status', 'admin_notes'])
    return item


def stop_live_session(session):
    """Emergency-stops an active broadcast using the same end_live_session()
    the owner's own "end stream" button and the stale-session cleanup task
    use — so viewers get the identical stream_ended event either way."""
    with transaction.atomic():
        end_live_session(session, reason='admin_stopped')
    return session
