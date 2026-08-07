"""
core.tasks.cleanup_orphaned_reel_uploads — the periodic safety net for an
upload flow abandoned mid-way (app closed after request_upload_url but
before finalize). Mirrors test_reels.py's pytest-django + mocked
core.aws_utils style — no real boto3/network call happens anywhere here.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from core.models import Reel, User, UserStore
from core.tasks import cleanup_orphaned_reel_uploads

pytestmark = pytest.mark.django_db


def make_user(username, **kwargs):
    return User.objects.create_user(username=username, password='pass12345', **kwargs)


def make_store(owner, **extra):
    return UserStore.objects.create(user=owner, name='store', **extra)


def make_pending_reel(store, *, created_at, thumbnail_key=''):
    reel = Reel.objects.create(
        store=store,
        video_key=f'incoming/{store.id}/abc-uuid/video.mp4',
        thumbnail_key=thumbnail_key,
        status='pending_upload',
        duration_seconds=0,
    )
    # created_at is auto_now_add — backdate it directly via update() so the
    # cleanup task's cutoff filter sees it as stale.
    Reel.objects.filter(id=reel.id).update(created_at=created_at)
    reel.refresh_from_db()
    return reel


@patch('core.aws_utils.delete_object')
def test_stale_pending_reel_is_purged_and_s3_keys_deleted(mock_delete):
    owner = make_user('cleanup_owner1')
    store = make_store(owner)
    stale = make_pending_reel(
        store,
        created_at=timezone.now() - timedelta(hours=3),
        thumbnail_key=f'incoming/{store.id}/abc-uuid/thumb.jpg',
    )

    result = cleanup_orphaned_reel_uploads()

    assert not Reel.objects.filter(id=stale.id).exists()
    assert mock_delete.call_count == 2
    mock_delete.assert_any_call(stale.video_key)
    mock_delete.assert_any_call(stale.thumbnail_key)
    assert '1' in result


@patch('core.aws_utils.delete_object')
def test_stale_pending_reel_without_thumbnail_only_deletes_video_key(mock_delete):
    owner = make_user('cleanup_owner2')
    store = make_store(owner)
    stale = make_pending_reel(store, created_at=timezone.now() - timedelta(hours=3))

    cleanup_orphaned_reel_uploads()

    assert not Reel.objects.filter(id=stale.id).exists()
    mock_delete.assert_called_once_with(stale.video_key)


@patch('core.aws_utils.delete_object')
def test_recent_pending_reel_is_not_touched(mock_delete):
    owner = make_user('cleanup_owner3')
    store = make_store(owner)
    fresh = make_pending_reel(store, created_at=timezone.now() - timedelta(minutes=30))

    cleanup_orphaned_reel_uploads()

    assert Reel.objects.filter(id=fresh.id).exists()
    mock_delete.assert_not_called()


@patch('core.aws_utils.delete_object')
def test_published_reel_is_never_touched_even_if_old(mock_delete):
    owner = make_user('cleanup_owner4')
    store = make_store(owner)
    old_published = Reel.objects.create(
        store=store,
        video_key=f'reels/{store.id}/abc-uuid/video.mp4',
        thumbnail_key=f'reels/{store.id}/abc-uuid/thumb.jpg',
        status='published',
        duration_seconds=15,
        published_at=timezone.now(),
    )
    Reel.objects.filter(id=old_published.id).update(
        created_at=timezone.now() - timedelta(days=30)
    )

    cleanup_orphaned_reel_uploads()

    assert Reel.objects.filter(id=old_published.id).exists()
    mock_delete.assert_not_called()


@patch('core.aws_utils.delete_object')
def test_s3_delete_failure_on_one_reel_does_not_block_others(mock_delete):
    """One reel's S3 delete_object raising shouldn't stop the sweep from
    still purging the rest — each reel is cleaned up independently."""
    owner_a = make_user('cleanup_owner5a')
    store_a = make_store(owner_a)
    owner_b = make_user('cleanup_owner5b')
    store_b = make_store(owner_b)

    failing = make_pending_reel(store_a, created_at=timezone.now() - timedelta(hours=5))
    ok = make_pending_reel(store_b, created_at=timezone.now() - timedelta(hours=5))

    def side_effect(key):
        if key == failing.video_key:
            raise Exception('S3 unavailable')
        return None

    mock_delete.side_effect = side_effect

    cleanup_orphaned_reel_uploads()

    # The failing reel's row is left in place (its cleanup raised before
    # reel.delete() ran) so the next sweep can retry it.
    assert Reel.objects.filter(id=failing.id).exists()
    assert not Reel.objects.filter(id=ok.id).exists()
