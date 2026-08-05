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


class ReportResolutionError(ValueError):
    """action_taken doesn't match this report's target (e.g. 'item_archived'
    on a report filed against a user) or resolve_report was asked to close
    the report out without deciding resolved vs. dismissed."""


def resolve_report(report, *, new_status, action_taken='none', admin_notes='', actor):
    """Admin decision on a Report: closes it out (resolved/dismissed) and,
    if warranted, applies the underlying moderation action to the reported
    item/store/user through the same functions the dedicated admin actions
    use — so a report resolution can never drift from what a direct
    suspend/archive would do. Raises ReportResolutionError if action_taken
    doesn't fit the report's target; nothing is written in that case."""
    if new_status not in ('resolved', 'dismissed'):
        raise ReportResolutionError("new_status must be 'resolved' or 'dismissed'")

    if action_taken == 'item_archived' and not report.reported_item_id:
        raise ReportResolutionError('This report is not against an item')
    if action_taken == 'store_suspended' and not report.reported_store_id:
        raise ReportResolutionError('This report is not against a store')
    if action_taken == 'user_suspended' and not report.reported_user_id:
        raise ReportResolutionError('This report is not against a user')

    with transaction.atomic():
        if action_taken == 'item_archived':
            archive_item(report.reported_item, reason=admin_notes or 'بلاغ من المستخدمين', actor=actor)
        elif action_taken == 'user_suspended':
            suspend_user(report.reported_user, reason=admin_notes or 'بلاغ من المستخدمين', actor=actor)
        elif action_taken == 'store_suspended':
            # UserStore has no admin_notes field to attribute a reason to —
            # matches the existing deactivate_stores_action in admin.py,
            # which also flips is_active with no reason trail.
            report.reported_store.is_active = False
            report.reported_store.save(update_fields=['is_active'])

        report.status = new_status
        report.action_taken = action_taken
        report.admin_notes = admin_notes
        report.reviewed_by = actor
        report.resolved_at = timezone.now()
        report.save(update_fields=[
            'status', 'action_taken', 'admin_notes', 'reviewed_by', 'resolved_at'
        ])
    return report
