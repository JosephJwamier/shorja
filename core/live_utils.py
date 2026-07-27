from django.utils import timezone
from .consumers import broadcast_live_event


def end_live_session(session, reason='manual'):
    """
    Ends a LiveSession: flips status, resets the live viewer count, skips
    any leftover queue entries, and broadcasts stream_ended. Shared by
    LiveSessionViewSet.end_session and the auto_end_stale_live_sessions
    Celery task so both end a session identically. Caller wraps this in an
    outer transaction.atomic().
    """
    session.status = 'ended'
    session.ended_at = timezone.now()
    session.current_viewer_count = 0
    session.save(update_fields=['status', 'ended_at', 'current_viewer_count'])

    session.items.filter(status__in=['queued', 'now_showing']).update(status='skipped')

    broadcast_live_event(session.id, 'stream_ended', {'reason': reason})
