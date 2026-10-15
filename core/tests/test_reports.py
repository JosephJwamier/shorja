"""
Report feature — users flagging items/stores/other users for violating
platform policy. Covers the create_report/resolve DRF actions, the
staff-vs-reporter queryset split, the raw-DELETE guard (same soft-delete
philosophy as Dispute — see test_delete_everywhere.py), and resolve_report()
in core/services/moderation.py applying the right moderation action for
each target type.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from core.models import Category, Item, Report, User, UserStore
from core.services.moderation import ReportResolutionError, resolve_report

pytestmark = pytest.mark.django_db


def make_user(username, **kwargs):
    return User.objects.create_user(username=username, password='pass12345', **kwargs)


def make_item(owner, **extra):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    return Item.objects.create(
        owner=owner, title='item', description='d', category=category,
        condition='used', quantity=1, status='active', **extra
    )


def make_store(owner, **extra):
    return UserStore.objects.create(user=owner, name='store', **extra)


@pytest.fixture
def client():
    return APIClient()


# ---------------------------------------------------------------------------
# create_report
# ---------------------------------------------------------------------------

def test_create_report_against_item(client):
    reporter = make_user('reporter1')
    owner = make_user('owner1')
    item = make_item(owner)
    client.force_authenticate(user=reporter)

    response = client.post('/api/reports/create_report/', {
        'target_type': 'item', 'target_id': item.id,
        'reason': 'counterfeit', 'description': 'fake brand',
    }, format='json')

    assert response.status_code == 201
    body = response.json()
    assert body['success'] is True
    assert body['data']['target_type'] == 'item'
    assert body['data']['reported_item']['id'] == item.id

    report = Report.objects.get()
    assert report.reporter == reporter
    assert report.reported_item == item
    assert report.status == 'pending'


def test_create_report_against_store(client):
    reporter = make_user('reporter2')
    owner = make_user('owner2')
    store = make_store(owner)
    client.force_authenticate(user=reporter)

    response = client.post('/api/reports/create_report/', {
        'target_type': 'store', 'target_id': store.id, 'reason': 'scam',
    }, format='json')

    assert response.status_code == 201
    report = Report.objects.get()
    assert report.reported_store == store
    assert report.target_type == 'store'


def test_create_report_against_user(client):
    reporter = make_user('reporter3')
    target = make_user('target3')
    client.force_authenticate(user=reporter)

    response = client.post('/api/reports/create_report/', {
        'target_type': 'user', 'target_id': target.id, 'reason': 'harassment',
    }, format='json')

    assert response.status_code == 201
    report = Report.objects.get()
    assert report.reported_user == target
    assert report.target_type == 'user'


def test_create_report_rejects_missing_target(client):
    reporter = make_user('reporter4')
    client.force_authenticate(user=reporter)

    response = client.post('/api/reports/create_report/', {
        'target_type': 'item', 'target_id': 999999, 'reason': 'spam',
    }, format='json')

    assert response.status_code == 400
    assert not Report.objects.exists()


def test_cannot_report_own_item(client):
    owner = make_user('owner5')
    item = make_item(owner)
    client.force_authenticate(user=owner)

    response = client.post('/api/reports/create_report/', {
        'target_type': 'item', 'target_id': item.id, 'reason': 'spam',
    }, format='json')

    assert response.status_code == 400
    assert not Report.objects.exists()


def test_cannot_report_self(client):
    user = make_user('self_reporter')
    client.force_authenticate(user=user)

    response = client.post('/api/reports/create_report/', {
        'target_type': 'user', 'target_id': user.id, 'reason': 'other',
    }, format='json')

    assert response.status_code == 400
    assert not Report.objects.exists()


def test_duplicate_open_report_on_same_target_is_rejected(client):
    reporter = make_user('reporter6')
    owner = make_user('owner6')
    item = make_item(owner)
    client.force_authenticate(user=reporter)

    first = client.post('/api/reports/create_report/', {
        'target_type': 'item', 'target_id': item.id, 'reason': 'spam',
    }, format='json')
    assert first.status_code == 201

    second = client.post('/api/reports/create_report/', {
        'target_type': 'item', 'target_id': item.id, 'reason': 'scam',
    }, format='json')
    assert second.status_code == 400
    assert Report.objects.filter(reporter=reporter, reported_item=item).count() == 1


def test_can_refile_report_after_previous_one_was_resolved(client):
    reporter = make_user('reporter7')
    owner = make_user('owner7')
    item = make_item(owner)
    staff = make_user('staff7', is_staff=True)

    first = Report.objects.create(reporter=reporter, reported_item=item, reason='spam')
    resolve_report(first, new_status='dismissed', actor=staff)

    client.force_authenticate(user=reporter)
    response = client.post('/api/reports/create_report/', {
        'target_type': 'item', 'target_id': item.id, 'reason': 'scam',
    }, format='json')

    assert response.status_code == 201
    assert Report.objects.filter(reporter=reporter, reported_item=item).count() == 2


# ---------------------------------------------------------------------------
# visibility — a reporter sees only their own filed reports, staff sees all
# ---------------------------------------------------------------------------

def test_reporter_only_sees_own_reports(client):
    reporter_a = make_user('vis_reporter_a')
    reporter_b = make_user('vis_reporter_b')
    owner = make_user('vis_owner')
    item = make_item(owner)
    Report.objects.create(reporter=reporter_a, reported_item=item, reason='spam')
    Report.objects.create(reporter=reporter_b, reported_item=item, reason='scam')

    client.force_authenticate(user=reporter_a)
    response = client.get('/api/reports/')

    assert response.status_code == 200
    results = response.json()['results'] if 'results' in response.json() else response.json()
    assert len(results) == 1
    assert results[0]['reporter']['username'] == 'vis_reporter_a'


def test_staff_sees_all_reports(client):
    reporter = make_user('vis_reporter_c')
    owner = make_user('vis_owner_c')
    staff = make_user('vis_staff_c', is_staff=True)
    item = make_item(owner)
    Report.objects.create(reporter=reporter, reported_item=item, reason='spam')

    client.force_authenticate(user=staff)
    response = client.get('/api/reports/')

    assert response.status_code == 200
    results = response.json()['results'] if 'results' in response.json() else response.json()
    assert len(results) == 1


# ---------------------------------------------------------------------------
# raw delete is blocked — same soft-delete philosophy as Dispute
# ---------------------------------------------------------------------------

def test_raw_report_delete_is_blocked_for_reporter_and_staff(client):
    reporter = make_user('del_reporter')
    owner = make_user('del_owner')
    staff = make_user('del_staff', is_staff=True)
    item = make_item(owner)
    report = Report.objects.create(reporter=reporter, reported_item=item, reason='spam')

    client.force_authenticate(user=reporter)
    response = client.delete(f'/api/reports/{report.id}/')
    assert response.status_code == 405

    client.force_authenticate(user=staff)
    response = client.delete(f'/api/reports/{report.id}/')
    assert response.status_code == 405

    assert Report.objects.filter(id=report.id).exists()


# ---------------------------------------------------------------------------
# resolve — DRF endpoint
# ---------------------------------------------------------------------------

def test_resolve_endpoint_rejects_non_staff(client):
    reporter = make_user('res_reporter1')
    owner = make_user('res_owner1')
    item = make_item(owner)
    report = Report.objects.create(reporter=reporter, reported_item=item, reason='spam')

    client.force_authenticate(user=reporter)
    response = client.post(f'/api/reports/{report.id}/resolve/', {
        'status': 'dismissed',
    }, format='json')

    assert response.status_code == 403


def test_resolve_endpoint_archives_reported_item(client):
    reporter = make_user('res_reporter2')
    owner = make_user('res_owner2')
    staff = make_user('res_staff2', is_staff=True)
    item = make_item(owner)
    report = Report.objects.create(reporter=reporter, reported_item=item, reason='counterfeit')

    client.force_authenticate(user=staff)
    response = client.post(f'/api/reports/{report.id}/resolve/', {
        'status': 'resolved', 'action_taken': 'item_archived', 'admin_notes': 'confirmed fake',
    }, format='json')

    assert response.status_code == 200
    item.refresh_from_db()
    report.refresh_from_db()
    assert item.status == 'archived'
    assert report.status == 'resolved'
    assert report.action_taken == 'item_archived'
    assert report.reviewed_by == staff
    assert report.resolved_at is not None


def test_resolve_endpoint_dismiss_takes_no_action(client):
    reporter = make_user('res_reporter3')
    owner = make_user('res_owner3')
    staff = make_user('res_staff3', is_staff=True)
    item = make_item(owner)
    report = Report.objects.create(reporter=reporter, reported_item=item, reason='other')

    client.force_authenticate(user=staff)
    response = client.post(f'/api/reports/{report.id}/resolve/', {
        'status': 'dismissed',
    }, format='json')

    assert response.status_code == 200
    item.refresh_from_db()
    report.refresh_from_db()
    assert item.status == 'active'
    assert report.status == 'dismissed'
    assert report.action_taken == 'none'


# ---------------------------------------------------------------------------
# resolve_report() service — action_taken must match the report's target
# ---------------------------------------------------------------------------

def test_resolve_report_suspends_reported_user():
    reporter = make_user('svc_reporter1')
    target = make_user('svc_target1')
    staff = make_user('svc_staff1', is_staff=True)
    report = Report.objects.create(reporter=reporter, reported_user=target, reason='harassment')

    resolve_report(report, new_status='resolved', action_taken='user_suspended',
                    admin_notes='repeated harassment', actor=staff)

    target.refresh_from_db()
    assert target.is_active is False
    assert 'repeated harassment' in target.admin_notes


def test_resolve_report_suspends_reported_store():
    reporter = make_user('svc_reporter2')
    owner = make_user('svc_owner2')
    staff = make_user('svc_staff2', is_staff=True)
    store = make_store(owner)
    report = Report.objects.create(reporter=reporter, reported_store=store, reason='scam')

    resolve_report(report, new_status='resolved', action_taken='store_suspended', actor=staff)

    store.refresh_from_db()
    assert store.is_active is False


def test_resolve_report_rejects_action_taken_mismatched_with_target():
    reporter = make_user('svc_reporter3')
    target = make_user('svc_target3')
    staff = make_user('svc_staff3', is_staff=True)
    report = Report.objects.create(reporter=reporter, reported_user=target, reason='harassment')

    with pytest.raises(ReportResolutionError):
        resolve_report(report, new_status='resolved', action_taken='item_archived', actor=staff)

    report.refresh_from_db()
    assert report.status == 'pending'  # nothing was written


def test_resolve_report_rejects_invalid_status():
    reporter = make_user('svc_reporter4')
    owner = make_user('svc_owner4')
    staff = make_user('svc_staff4', is_staff=True)
    item = make_item(owner)
    report = Report.objects.create(reporter=reporter, reported_item=item, reason='spam')

    with pytest.raises(ReportResolutionError):
        resolve_report(report, new_status='pending', actor=staff)


# ---------------------------------------------------------------------------
# model constraint — exactly one target must be set
# ---------------------------------------------------------------------------

def test_report_requires_exactly_one_target():
    reporter = make_user('constraint_reporter')
    with pytest.raises(Exception):
        Report.objects.create(reporter=reporter, reason='other')
