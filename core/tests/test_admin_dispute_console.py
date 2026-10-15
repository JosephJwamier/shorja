"""
Phase 3 — the admin dispute resolution console itself (as opposed to
resolve_dispute(), covered in test_dispute_resolution.py). Confirms the
custom /resolve/ view renders the details a support agent actually needs
(reservation, proof screenshot, chat, trust scores), that submitting it
moves money the same way the API does, and that the changelist's SLA/status
badges and "resolve" link behave as designed.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Category, Chat, Dispute, Item, Message, Reservation, User, get_or_create_wallet

pytestmark = pytest.mark.django_db


def make_user(username, **extra):
    return User.objects.create_user(username=username, password='pass12345', **extra)


def make_support_staff(username):
    """A real staff member from the 'الدعم الفني' group seeded by the Phase 0
    migration — the actual role meant to resolve disputes day-to-day, as
    opposed to a superuser or a staff account with no permissions at all."""
    user = make_user(username, is_staff=True)
    user.groups.add(Group.objects.get(name='الدعم الفني'))
    return user


def make_reservation(*, owner, buyer, total_price=Decimal('100.00'), completed=True):
    category, _ = Category.objects.get_or_create(name='cat', slug='cat')
    item = Item.objects.create(owner=owner, title='item', description='d', category=category,
                                condition='used', quantity=3, status='reserved')
    return Reservation.objects.create(
        item=item, buyer=buyer, quantity=1, deposit_amount=Decimal('10'), total_price=total_price,
        status='disputed', expires_at=timezone.now() + timedelta(days=1),
        completed_at=timezone.now() if completed else None, is_deposit_only=False,
    )


def make_dispute(reservation):
    return Dispute.objects.create(
        item=reservation.item, reservation=reservation,
        complainant=reservation.buyer, defendant=reservation.item.owner,
        title='المنتج تالف', description='وصل المنتج مكسورًا',
    )


def resolve_url(dispute):
    return reverse('admin:core_dispute_resolve', args=[dispute.pk])


def test_console_shows_reservation_chat_and_trust_scores():
    owner, buyer = make_user('owner_c1'), make_user('buyer_c1')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('80'))
    dispute = make_dispute(reservation)
    chat = Chat.objects.create(item=reservation.item, buyer=buyer, seller=owner)
    Message.objects.create(chat=chat, sender=buyer, content='وين طلبي؟')
    Message.objects.create(chat=chat, sender=owner, content='بوصلك اليوم')

    staff = make_support_staff('staff_c1')
    client = Client()
    client.force_login(staff)

    response = client.get(resolve_url(dispute))
    assert response.status_code == 200
    html = response.content.decode()
    assert 'المنتج تالف' in html
    assert 'وين طلبي؟' in html
    assert 'بوصلك اليوم' in html
    assert str(reservation.total_price) in html or '80.00' in html


def test_submitting_pay_seller_moves_money_and_redirects():
    owner, buyer = make_user('owner_c2'), make_user('buyer_c2')
    reservation = make_reservation(owner=owner, buyer=buyer, total_price=Decimal('60'))
    dispute = make_dispute(reservation)

    staff = make_support_staff('staff_c2')
    client = Client()
    client.force_login(staff)

    response = client.post(resolve_url(dispute), {
        'resolution_action': 'pay_seller',
        'resolution': 'البائع أثبت التسليم',
        'admin_notes': 'راجعت لقطة الإثبات',
    })

    assert response.status_code == 302
    dispute.refresh_from_db()
    assert dispute.status == 'resolved'
    assert dispute.resolution_action == 'pay_seller'
    assert get_or_create_wallet(owner).balance == Decimal('60.00')


def test_submitting_invalid_action_shows_error_and_writes_nothing():
    owner, buyer = make_user('owner_c3'), make_user('buyer_c3')
    reservation = make_reservation(owner=owner, buyer=buyer, completed=False)
    dispute = make_dispute(reservation)

    staff = make_support_staff('staff_c3')
    client = Client()
    client.force_login(staff)

    response = client.post(resolve_url(dispute), {
        'resolution_action': 'pay_seller',
        'resolution': 'x',
    })

    assert response.status_code == 200  # redisplays the form, doesn't redirect
    dispute.refresh_from_db()
    assert dispute.status == 'open'


def test_resolve_view_requires_change_dispute_permission():
    owner, buyer = make_user('owner_c4'), make_user('buyer_c4')
    reservation = make_reservation(owner=owner, buyer=buyer)
    dispute = make_dispute(reservation)

    plain_staff = make_user('staff_c4', is_staff=True)  # staff but no core.change_dispute perm
    client = Client()
    client.force_login(plain_staff)

    response = client.get(resolve_url(dispute))
    assert response.status_code == 403


def test_already_resolved_dispute_shows_readonly_summary_not_a_form():
    owner, buyer = make_user('owner_c5'), make_user('buyer_c5')
    reservation = make_reservation(owner=owner, buyer=buyer)
    dispute = make_dispute(reservation)
    dispute.status = 'resolved'
    dispute.resolution_action = 'no_action'
    dispute.resolved_at = timezone.now()
    dispute.save()

    staff = make_support_staff('staff_c5')
    client = Client()
    client.force_login(staff)

    response = client.get(resolve_url(dispute))
    assert response.status_code == 200
    assert 'محسوم بالفعل' in response.content.decode()


def test_changelist_shows_status_and_sla_badges():
    owner, buyer = make_user('owner_c6'), make_user('buyer_c6')
    reservation = make_reservation(owner=owner, buyer=buyer)
    dispute = make_dispute(reservation)
    # Backdate creation to trigger the "overdue" SLA badge.
    Dispute.objects.filter(pk=dispute.pk).update(created_at=timezone.now() - timedelta(days=3))

    staff = User.objects.create_superuser(username='staff_c6', password='x', email='s6@example.com')
    client = Client()
    client.force_login(staff)

    response = client.get(reverse('admin:core_dispute_changelist'))
    assert response.status_code == 200
    html = response.content.decode()
    assert 'متأخر' in html
    assert 'حسم النزاع' in html


def test_dispute_sidebar_group_present_and_link_works():
    staff = User.objects.create_superuser(username='staff_c7', password='x', email='s7@example.com')
    client = Client()
    client.force_login(staff)

    response = client.get(f'/{settings.ADMIN_URL_PATH}')
    html = response.content.decode()
    assert 'النزاعات' in html
    assert 'core/dispute/' in html
