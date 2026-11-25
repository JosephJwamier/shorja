"""A double tap is two requests at once, each of which looks before the other has
written. For the few actions where "is there one already?" decides, the second
waits for the first to finish and then sees what it made.

The cache holds the lock rather than a database row: these views send tasks and
pushes as they go, and inside one long transaction those would run before the
row they are about is there.
"""
import functools
import logging
import time
import uuid

from django.core.cache import cache

logger = logging.getLogger(__name__)

HOLD_SECONDS = 20
WAIT_SECONDS = 10


def one_at_a_time(name):
    """Decorates a viewset action: one of these per user at a time."""
    def wrap(view):
        @functools.wraps(view)
        def wrapped(self, request, *args, **kwargs):
            key, mine = f'one:{name}:{request.user.pk}', uuid.uuid4().hex
            try:
                until = time.monotonic() + WAIT_SECONDS
                while not cache.add(key, mine, HOLD_SECONDS) and time.monotonic() < until:
                    time.sleep(0.05)
            except Exception:  # no cache is no reason to refuse the action
                logger.warning('one_at_a_time skipped', exc_info=True)
            try:
                return view(self, request, *args, **kwargs)
            finally:
                try:
                    if cache.get(key) == mine:
                        cache.delete(key)
                except Exception:
                    logger.warning('one_at_a_time not released', exc_info=True)
        return wrapped
    return wrap
