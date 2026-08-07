"""
Reels feature — presigned-upload flow (request_upload_url/finalize), the
feed/like/comment/destroy endpoints, and the Report model's extension to
cover reel targets. Mirrors core/tests/test_reports.py's pytest-django +
APIClient style. Every AWS S3 call is mocked at the core.aws_utils boundary
— no real boto3/network call happens anywhere in this file.
"""
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Category, Item, Reel, ReelLike, Report, User, UserStore
from core.services.moderation import ReportResolutionError, resolve_report

pytestmark = pytest.mark.django_db


def make_user(username, **kwargs):
    return User.objects.create_user(username=username, password='pass12345', **kwargs)


def make_store(owner, **extra):
    return UserStore.objects.create(user=owner, name='store', **extra)


def make_item(owner, **extra):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return Item.objects.create(
        owner=owner, title='item', description='d', category=category,
        condition='good', quantity=1, status='active', **extra
    )


def make_pending_reel(store, **extra):
    defaults = dict(
        store=store, video_key=f'incoming/{store.id}/abc-uuid/video.mp4',
        thumbnail_key='', status='pending_upload', duration_seconds=0,
    )
    defaults.update(extra)
    return Reel.objects.create(**defaults)


def make_published_reel(store, **extra):
    defaults = dict(
        store=store, video_key=f'reels/{store.id}/abc-uuid/video.mp4',
        thumbnail_key=f'reels/{store.id}/abc-uuid/thumb.jpg',
        status='published', duration_seconds=15, published_at=timezone.now(),
    )
    defaults.update(extra)
    return Reel.objects.create(**defaults)


@pytest.fixture
def client():
    return APIClient()


# ---------------------------------------------------------------------------
# request_upload_url
# ---------------------------------------------------------------------------

@patch('core.aws_utils.build_presigned_put_url')
def test_request_upload_url_creates_pending_reel(mock_presign, client):
    mock_presign.return_value = 'https://s3.example.com/signed-put-url'
    owner = make_user('ru_owner1')
    store = make_store(owner)
    client.force_authenticate(user=owner)

    response = client.post('/api/reels/request_upload_url/', {
        'content_type': 'video/mp4', 'has_thumbnail': True,
    }, format='json')

    assert response.status_code == 201
    body = response.json()
    assert body['success'] is True
    data = body['data']
    assert data['video_upload_url'] == 'https://s3.example.com/signed-put-url'
    assert data['thumbnail_upload_url'] == 'https://s3.example.com/signed-put-url'
    assert data['video_key'].startswith(f'incoming/{store.id}/')
    assert data['thumbnail_key'].startswith(f'incoming/{store.id}/')
    assert data['expires_in'] == 900

    reel = Reel.objects.get(id=data['reel_id'])
    assert reel.store == store
    assert reel.status == 'pending_upload'
    assert reel.video_key == data['video_key']
    assert mock_presign.call_count == 2  # video + thumbnail


def test_request_upload_url_rejects_non_video_content_type(client):
    owner = make_user('ru_owner2')
    make_store(owner)
    client.force_authenticate(user=owner)

    response = client.post('/api/reels/request_upload_url/', {
        'content_type': 'image/png',
    }, format='json')

    assert response.status_code == 400
    assert not Reel.objects.exists()


def test_request_upload_url_rejects_non_store_user(client):
    """IsStoreOwner — a plain authenticated user with no store gets 403."""
    user = make_user('ru_no_store')
    client.force_authenticate(user=user)

    response = client.post('/api/reels/request_upload_url/', {
        'content_type': 'video/mp4',
    }, format='json')

    assert response.status_code == 403
    assert not Reel.objects.exists()


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------

@patch('core.aws_utils.head_object')
def test_finalize_rejects_missing_video(mock_head, client):
    mock_head.return_value = None
    owner = make_user('fin_owner1')
    store = make_store(owner)
    reel = make_pending_reel(store)
    client.force_authenticate(user=owner)

    response = client.post(f'/api/reels/{reel.id}/finalize/', {
        'duration_seconds': 10,
    }, format='json')

    assert response.status_code == 400
    reel.refresh_from_db()
    assert reel.status == 'pending_upload'


@patch('core.aws_utils.head_object')
def test_finalize_rejects_zero_size_video(mock_head, client):
    mock_head.return_value = {'exists': True, 'size': 0, 'content_type': 'video/mp4'}
    owner = make_user('fin_owner2')
    store = make_store(owner)
    reel = make_pending_reel(store)
    client.force_authenticate(user=owner)

    response = client.post(f'/api/reels/{reel.id}/finalize/', {
        'duration_seconds': 10,
    }, format='json')

    assert response.status_code == 400
    reel.refresh_from_db()
    assert reel.status == 'pending_upload'


@patch('core.tasks.notify_store_followers_reel.delay')
@patch('core.aws_utils.delete_object')
@patch('core.aws_utils.copy_object')
@patch('core.aws_utils.head_object')
def test_finalize_success_publishes_reel_and_notifies(
    mock_head, mock_copy, mock_delete, mock_notify_delay, client
):
    mock_head.return_value = {'exists': True, 'size': 555000, 'content_type': 'video/mp4'}
    owner = make_user('fin_owner3')
    store = make_store(owner)
    item = make_item(owner)
    reel = make_pending_reel(store, thumbnail_key=f'incoming/{store.id}/abc-uuid/thumb.jpg')
    client.force_authenticate(user=owner)

    response = client.post(f'/api/reels/{reel.id}/finalize/', {
        'caption': 'شاهد منتجنا الجديد', 'item_id': item.id, 'duration_seconds': 22,
    }, format='json')

    assert response.status_code == 200
    assert response.json()['data']['status'] == 'published'

    reel.refresh_from_db()
    assert reel.status == 'published'
    assert reel.published_at is not None
    assert reel.caption == 'شاهد منتجنا الجديد'
    assert reel.item_id == item.id
    assert reel.duration_seconds == 22
    assert reel.file_size_bytes == 555000
    assert reel.video_key.startswith(f'reels/{store.id}/')
    assert reel.thumbnail_key.startswith(f'reels/{store.id}/')

    assert mock_copy.call_count == 2  # video + thumbnail
    assert mock_delete.call_count == 2  # cleans up both incoming/ copies
    mock_notify_delay.assert_called_once_with(reel.id)


@patch('core.aws_utils.head_object')
def test_finalize_rejects_tagging_another_owners_item(mock_head, client):
    mock_head.return_value = {'exists': True, 'size': 555000, 'content_type': 'video/mp4'}
    owner = make_user('fin_owner4')
    store = make_store(owner)
    other_owner = make_user('fin_other_owner')
    other_item = make_item(other_owner)
    reel = make_pending_reel(store)
    client.force_authenticate(user=owner)

    response = client.post(f'/api/reels/{reel.id}/finalize/', {
        'item_id': other_item.id, 'duration_seconds': 10,
    }, format='json')

    assert response.status_code == 400
    reel.refresh_from_db()
    assert reel.status == 'pending_upload'


@patch('core.aws_utils.head_object')
def test_finalize_rejects_another_stores_reel(mock_head, client):
    """IsStoreOwner object-level check — store A cannot finalize store B's reel."""
    mock_head.return_value = {'exists': True, 'size': 555000, 'content_type': 'video/mp4'}
    owner_a = make_user('fin_owner5a')
    make_store(owner_a)
    owner_b = make_user('fin_owner5b')
    store_b = make_store(owner_b)
    reel = make_pending_reel(store_b)

    client.force_authenticate(user=owner_a)
    response = client.post(f'/api/reels/{reel.id}/finalize/', {
        'duration_seconds': 10,
    }, format='json')

    assert response.status_code == 403
    reel.refresh_from_db()
    assert reel.status == 'pending_upload'


# ---------------------------------------------------------------------------
# like / unlike
# ---------------------------------------------------------------------------

def test_like_and_unlike_reel_updates_counter(client):
    owner = make_user('like_owner')
    store = make_store(owner)
    reel = make_published_reel(store)
    liker = make_user('like_liker')
    client.force_authenticate(user=liker)

    response = client.post(f'/api/reels/{reel.id}/like/')
    assert response.status_code == 200
    assert response.json()['likes_count'] == 1
    reel.refresh_from_db()
    assert reel.likes_count == 1
    assert ReelLike.objects.filter(reel=reel, user=liker).exists()

    # Liking again is idempotent (get_or_create) — counter doesn't double-bump.
    response = client.post(f'/api/reels/{reel.id}/like/')
    assert response.status_code == 200
    assert response.json()['likes_count'] == 1

    response = client.post(f'/api/reels/{reel.id}/unlike/')
    assert response.status_code == 200
    assert response.json()['likes_count'] == 0
    reel.refresh_from_db()
    assert reel.likes_count == 0
    assert not ReelLike.objects.filter(reel=reel, user=liker).exists()


def test_unlike_without_a_prior_like_does_not_go_negative(client):
    owner = make_user('like_owner2')
    store = make_store(owner)
    reel = make_published_reel(store)
    user = make_user('like_noop_user')
    client.force_authenticate(user=user)

    response = client.post(f'/api/reels/{reel.id}/unlike/')
    assert response.status_code == 200
    assert response.json()['likes_count'] == 0
    reel.refresh_from_db()
    assert reel.likes_count == 0


def test_like_requires_authentication(client):
    owner = make_user('like_owner3')
    store = make_store(owner)
    reel = make_published_reel(store)

    response = client.post(f'/api/reels/{reel.id}/like/')
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# destroy — soft delete only
# ---------------------------------------------------------------------------

@patch('core.aws_utils.delete_object')
def test_destroy_hard_deletes_reel_and_purges_s3(mock_delete, client):
    owner = make_user('destroy_owner')
    store = make_store(owner)
    reel = make_published_reel(store)
    video_key, thumbnail_key = reel.video_key, reel.thumbnail_key
    client.force_authenticate(user=owner)

    response = client.delete(f'/api/reels/{reel.id}/')

    assert response.status_code == 200
    assert not Reel.objects.filter(id=reel.id).exists()
    assert mock_delete.call_count == 2
    mock_delete.assert_any_call(video_key)
    mock_delete.assert_any_call(thumbnail_key)


@patch('core.aws_utils.delete_object')
def test_destroy_keeps_tombstone_when_reel_was_reported(mock_delete, client):
    """A reported reel can't be hard-deleted — Report.reported_reel is
    CASCADE, so the row has to survive or the moderation trail goes with it.
    The media is purged from S3 all the same."""
    owner = make_user('destroy_owner_reported')
    store = make_store(owner)
    reel = make_published_reel(store)
    reporter = make_user('destroy_reporter')
    report = Report.objects.create(reporter=reporter, reported_reel=reel, reason='inappropriate')
    client.force_authenticate(user=owner)

    response = client.delete(f'/api/reels/{reel.id}/')

    assert response.status_code == 200
    assert Report.objects.filter(id=report.id).exists()
    reel.refresh_from_db()
    assert reel.status == 'deleted'
    assert reel.video_key == '' and reel.thumbnail_key == ''
    assert mock_delete.call_count == 2


@patch('core.aws_utils.delete_object')
def test_deleted_tombstone_is_hidden_from_my_reels(mock_delete, client):
    owner = make_user('destroy_owner_hidden')
    store = make_store(owner)
    reel = make_published_reel(store)
    kept = make_published_reel(store)
    reporter = make_user('destroy_reporter2')
    Report.objects.create(reporter=reporter, reported_reel=reel, reason='inappropriate')
    client.force_authenticate(user=owner)

    client.delete(f'/api/reels/{reel.id}/')
    response = client.get('/api/reels/my_reels/')

    assert response.status_code == 200
    returned_ids = {row['id'] for row in response.data['results']}
    assert returned_ids == {kept.id}


@patch('core.aws_utils.delete_object')
def test_destroy_rejects_non_owner(mock_delete, client):
    owner = make_user('destroy_owner2')
    store = make_store(owner)
    reel = make_published_reel(store)
    other = make_user('destroy_other')
    make_store(other)
    client.force_authenticate(user=other)

    response = client.delete(f'/api/reels/{reel.id}/')

    assert response.status_code == 403
    reel.refresh_from_db()
    assert reel.status == 'published'
    mock_delete.assert_not_called()


# ---------------------------------------------------------------------------
# View counting
# ---------------------------------------------------------------------------

def test_view_from_a_stranger_is_counted(client):
    owner = make_user('view_owner')
    store = make_store(owner)
    reel = make_published_reel(store)
    viewer = make_user('view_stranger')
    client.force_authenticate(user=viewer)

    response = client.post(f'/api/reels/{reel.id}/view/')

    assert response.status_code == 200
    assert response.data['counted'] is True
    reel.refresh_from_db()
    assert reel.view_count == 1


def test_view_from_the_reels_own_owner_is_not_counted(client):
    """The owner isn't audience — otherwise they inflate the number they use
    to judge how the reel performed, and can inflate it at will."""
    owner = make_user('view_owner2')
    store = make_store(owner)
    reel = make_published_reel(store)
    client.force_authenticate(user=owner)

    response = client.post(f'/api/reels/{reel.id}/view/')

    assert response.status_code == 200
    assert response.data['counted'] is False
    reel.refresh_from_db()
    assert reel.view_count == 0


def test_view_from_anonymous_is_counted(client):
    owner = make_user('view_owner3')
    store = make_store(owner)
    reel = make_published_reel(store)

    response = client.post(f'/api/reels/{reel.id}/view/')

    assert response.status_code == 200
    reel.refresh_from_db()
    assert reel.view_count == 1


# ---------------------------------------------------------------------------
# Owner edit (PATCH) — caption + tagged item
# ---------------------------------------------------------------------------

def test_owner_can_patch_caption_and_tagged_item(client):
    owner = make_user('patch_owner')
    store = make_store(owner)
    item = make_item(owner)
    reel = make_published_reel(store, caption='قديم')
    client.force_authenticate(user=owner)

    response = client.patch(
        f'/api/reels/{reel.id}/',
        {'caption': 'جديد', 'item_id': item.id},
        format='json',
    )

    assert response.status_code == 200
    reel.refresh_from_db()
    assert reel.caption == 'جديد'
    assert reel.item_id == item.id


def test_owner_can_clear_tagged_item_with_null(client):
    owner = make_user('patch_owner_clear')
    store = make_store(owner)
    item = make_item(owner)
    reel = make_published_reel(store, item=item)
    client.force_authenticate(user=owner)

    response = client.patch(f'/api/reels/{reel.id}/', {'item_id': None}, format='json')

    assert response.status_code == 200
    reel.refresh_from_db()
    assert reel.item_id is None


def test_patch_rejects_tagging_someone_elses_item(client):
    owner = make_user('patch_owner2')
    store = make_store(owner)
    stranger = make_user('patch_stranger')
    foreign_item = make_item(stranger)
    reel = make_published_reel(store)
    client.force_authenticate(user=owner)

    response = client.patch(
        f'/api/reels/{reel.id}/', {'item_id': foreign_item.id}, format='json'
    )

    assert response.status_code == 400
    reel.refresh_from_db()
    assert reel.item_id is None


def test_patch_rejects_non_owner(client):
    owner = make_user('patch_owner3')
    store = make_store(owner)
    reel = make_published_reel(store, caption='أصلي')
    other = make_user('patch_other')
    make_store(other)
    client.force_authenticate(user=other)

    response = client.patch(f'/api/reels/{reel.id}/', {'caption': 'مخترق'}, format='json')

    assert response.status_code == 403
    reel.refresh_from_db()
    assert reel.caption == 'أصلي'


def test_my_reels_includes_caption_for_editing(client):
    owner = make_user('my_reels_caption_owner')
    store = make_store(owner)
    make_published_reel(store, caption='وصف الريل')
    client.force_authenticate(user=owner)

    response = client.get('/api/reels/my_reels/')

    assert response.status_code == 200
    assert response.data['results'][0]['caption'] == 'وصف الريل'


# ---------------------------------------------------------------------------
# Report — reel target extension
# ---------------------------------------------------------------------------

def test_report_with_only_reported_reel_satisfies_constraint():
    reporter = make_user('rep_reporter1')
    owner = make_user('rep_owner1')
    store = make_store(owner)
    reel = make_published_reel(store)

    report = Report.objects.create(reporter=reporter, reported_reel=reel, reason='inappropriate')

    assert report.target_type == 'reel'
    assert report.target == reel


def test_report_mixing_reel_with_another_target_is_rejected():
    reporter = make_user('rep_reporter2')
    owner = make_user('rep_owner2')
    store = make_store(owner)
    reel = make_published_reel(store)
    item = make_item(owner)

    with pytest.raises(IntegrityError):
        Report.objects.create(
            reporter=reporter, reported_reel=reel, reported_item=item, reason='spam',
        )


def test_create_report_against_reel_via_api(client):
    reporter = make_user('rep_reporter3')
    owner = make_user('rep_owner3')
    store = make_store(owner)
    reel = make_published_reel(store)
    client.force_authenticate(user=reporter)

    response = client.post('/api/reports/create_report/', {
        'target_type': 'reel', 'target_id': reel.id, 'reason': 'inappropriate',
    }, format='json')

    assert response.status_code == 201
    report = Report.objects.get()
    assert report.reported_reel == reel
    assert report.target_type == 'reel'


def test_store_owner_cannot_report_own_reel(client):
    owner = make_user('rep_owner4')
    store = make_store(owner)
    reel = make_published_reel(store)
    client.force_authenticate(user=owner)

    response = client.post('/api/reports/create_report/', {
        'target_type': 'reel', 'target_id': reel.id, 'reason': 'other',
    }, format='json')

    assert response.status_code == 400
    assert not Report.objects.exists()


def test_resolve_report_reel_archived_round_trips_through_archive_reel():
    reporter = make_user('rep_reporter5')
    owner = make_user('rep_owner5')
    store = make_store(owner)
    reel = make_published_reel(store)
    staff = make_user('rep_staff5', is_staff=True)
    report = Report.objects.create(reporter=reporter, reported_reel=reel, reason='inappropriate')

    resolve_report(
        report, new_status='resolved', action_taken='reel_archived',
        admin_notes='محتوى مخالف', actor=staff,
    )

    reel.refresh_from_db()
    report.refresh_from_db()
    assert reel.status == 'archived'
    assert 'محتوى مخالف' in reel.admin_notes
    assert report.status == 'resolved'
    assert report.action_taken == 'reel_archived'


def test_resolve_report_reel_archived_rejects_mismatched_target():
    reporter = make_user('rep_reporter6')
    target_user = make_user('rep_target6')
    staff = make_user('rep_staff6', is_staff=True)
    report = Report.objects.create(reporter=reporter, reported_user=target_user, reason='harassment')

    with pytest.raises(ReportResolutionError):
        resolve_report(report, new_status='resolved', action_taken='reel_archived', actor=staff)

    report.refresh_from_db()
    assert report.status == 'pending'  # nothing was written
