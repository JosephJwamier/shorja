"""
Audit follow-up to test_item_reservation_deletion.py: several other
ViewSets had either (a) an unguarded default hard-delete that would CASCADE
through financial/legal/history records, or (b) no delete capability at all
where one was safe and missing. This locks in the fix for each:

- User/Chat/UserStore/ItemTransfer: raw DELETE now blocked (405), pointing
  at the existing safe action (delete-account / archive / toggle_active /
  reject|cancel) that already did the right thing.
- Dispute: raw DELETE blocked outright — no self-service equivalent needed.
- Category/StoreCoupon: DELETE now soft-deletes (is_active=False) instead
  of a hard delete that would cascade or (for Category) hit Item's PROTECT.
- Rating: was missing an ownership check entirely — any authenticated user
  could update/delete anyone else's rating.
- Notification: was fully read-only; now deletable (safe — nothing
  references it) plus a clear-all bulk action.
"""
from decimal import Decimal

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from core.models import (
    Category, Chat, Dispute, Item, ItemTransfer, Message, Notification,
    Rating, StoreCoupon, User, UserStore,
)

pytestmark = pytest.mark.django_db


def make_user(username, **kwargs):
    return User.objects.create_user(username=username, password='pass12345', **kwargs)


def make_category(name='cat'):
    category, _ = Category.objects.get_or_create(name=name, slug=name)
    return category


def make_item(owner):
    return Item.objects.create(
        owner=owner, title='item', description='d', category=make_category(),
        condition='used', quantity=1, status='active',
    )


@pytest.fixture
def client():
    return APIClient()


# ---- User ----

def test_raw_user_delete_is_blocked(client):
    user = make_user('raw_del_user')
    client.force_authenticate(user=user)

    response = client.delete(f'/api/users/{user.id}/')

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert User.objects.filter(id=user.id).exists()


# ---- Chat ----

def test_raw_chat_delete_is_blocked(client):
    buyer = make_user('chat_buyer')
    seller = make_user('chat_seller')
    item = make_item(seller)
    chat = Chat.objects.create(item=item, buyer=buyer, seller=seller)
    client.force_authenticate(user=buyer)

    response = client.delete(f'/api/chats/{chat.id}/')

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert Chat.objects.filter(id=chat.id).exists()


# ---- UserStore ----

def test_raw_store_delete_is_blocked(client):
    owner = make_user('store_owner')
    store = UserStore.objects.create(user=owner, name='My Store')
    client.force_authenticate(user=owner)

    response = client.delete(f'/api/stores/{store.id}/')

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert UserStore.objects.filter(id=store.id).exists()


# ---- ItemTransfer ----

def test_raw_transfer_delete_is_blocked(client):
    owner = make_user('transfer_owner')
    reseller = make_user('transfer_reseller')
    item = make_item(owner)
    transfer = ItemTransfer.objects.create(item=item, from_user=owner, to_user=reseller, quantity=1)
    client.force_authenticate(user=owner)

    response = client.delete(f'/api/transfers/{transfer.id}/')

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
    assert ItemTransfer.objects.filter(id=transfer.id).exists()


# ---- Dispute ----

def test_raw_dispute_delete_is_blocked_for_complainant_and_staff(client):
    complainant = make_user('dispute_complainant')
    defendant = make_user('dispute_defendant')
    item = make_item(defendant)
    dispute = Dispute.objects.create(
        item=item, complainant=complainant, defendant=defendant,
        title='t', description='d',
    )

    client.force_authenticate(user=complainant)
    response = client.delete(f'/api/disputes/{dispute.id}/')
    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    staff = make_user('dispute_staff', is_staff=True)
    client.force_authenticate(user=staff)
    response = client.delete(f'/api/disputes/{dispute.id}/')
    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    assert Dispute.objects.filter(id=dispute.id).exists()


# ---- Category ----

def test_category_delete_soft_deletes_instead_of_hard_deleting(client):
    superadmin = make_user('cat_superadmin', is_superuser=True, is_staff=True)
    category = make_category('deletable-cat')
    client.force_authenticate(user=superadmin)

    response = client.delete(f'/api/categories/{category.id}/')

    assert response.status_code == status.HTTP_204_NO_CONTENT
    category.refresh_from_db()
    assert category.is_active is False


def test_non_superadmin_cannot_delete_category(client):
    regular = make_user('cat_regular')
    category = make_category('protected-cat')
    client.force_authenticate(user=regular)

    response = client.delete(f'/api/categories/{category.id}/')

    assert response.status_code == status.HTTP_403_FORBIDDEN
    category.refresh_from_db()
    assert category.is_active is True


# ---- Rating ----

def test_rating_cannot_be_deleted_by_non_rater(client):
    rater = make_user('rating_rater')
    other = make_user('rating_other')
    rated = make_user('rating_rated')
    rating = Rating.objects.create(rater=rater, rated_user=rated, rating_type='buyer', score=4)
    client.force_authenticate(user=other)

    response = client.delete(f'/api/ratings/{rating.id}/')

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert Rating.objects.filter(id=rating.id).exists()


def test_rating_can_be_deleted_by_rater(client):
    rater = make_user('rating_rater2')
    rated = make_user('rating_rated2')
    rating = Rating.objects.create(rater=rater, rated_user=rated, rating_type='buyer', score=4)
    client.force_authenticate(user=rater)

    response = client.delete(f'/api/ratings/{rating.id}/')

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert not Rating.objects.filter(id=rating.id).exists()


# ---- StoreCoupon ----

def test_store_coupon_delete_soft_deletes_instead_of_hard_deleting(client):
    owner = make_user('coupon_owner')
    store = UserStore.objects.create(user=owner, name='Coupon Store')
    coupon = StoreCoupon.objects.create(store=store, code='SAVE10', amount=Decimal('10.00'))
    client.force_authenticate(user=owner)

    response = client.delete(f'/api/stores/{store.id}/coupons/{coupon.id}/')

    assert response.status_code == status.HTTP_200_OK
    coupon.refresh_from_db()  # would raise DoesNotExist if hard-deleted
    assert coupon.is_active is False


# ---- Notification ----

def test_notification_can_be_deleted_by_owner(client):
    user = make_user('notif_user')
    notification = Notification.objects.create(user=user, title='t', body='b')
    client.force_authenticate(user=user)

    response = client.delete(f'/api/notifications/{notification.id}/')

    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert not Notification.objects.filter(id=notification.id).exists()


def test_notification_cannot_be_deleted_by_another_user(client):
    owner = make_user('notif_owner')
    other = make_user('notif_other')
    notification = Notification.objects.create(user=owner, title='t', body='b')
    client.force_authenticate(user=other)

    response = client.delete(f'/api/notifications/{notification.id}/')

    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert Notification.objects.filter(id=notification.id).exists()


def test_clear_all_notifications(client):
    user = make_user('notif_clear_user')
    other = make_user('notif_clear_other')
    Notification.objects.create(user=user, title='t1', body='b1')
    Notification.objects.create(user=user, title='t2', body='b2')
    Notification.objects.create(user=other, title='t3', body='b3')
    client.force_authenticate(user=user)

    response = client.delete('/api/notifications/clear-all/')

    assert response.status_code == status.HTTP_200_OK
    assert not Notification.objects.filter(user=user).exists()
    assert Notification.objects.filter(user=other).exists()


# ---- Message deletion must actually stay hidden on refetch ----
#
# MessageViewSet.destroy() sets is_deleted_by_sender=True, but neither the
# ChatDetailSerializer's message list nor the older-messages pagination
# action filtered on that flag — so a "deleted" message reappeared the next
# time the chat was fetched. Fixed by excluding it for the sender specifically.

def test_deleted_message_disappears_for_sender_but_not_for_receiver(client):
    buyer = make_user('msg_buyer')
    seller = make_user('msg_seller')
    item = make_item(seller)
    chat = Chat.objects.create(item=item, buyer=buyer, seller=seller)
    message = Message.objects.create(chat=chat, sender=buyer, content='hello')

    client.force_authenticate(user=buyer)
    delete_response = client.delete(f'/api/messages/{message.id}/')
    assert delete_response.status_code == status.HTTP_200_OK

    refetch_as_sender = client.get(f'/api/chats/{chat.id}/')
    message_ids = [m['id'] for m in refetch_as_sender.data['messages']]
    assert message.id not in message_ids

    client.force_authenticate(user=seller)
    refetch_as_receiver = client.get(f'/api/chats/{chat.id}/')
    message_ids = [m['id'] for m in refetch_as_receiver.data['messages']]
    assert message.id in message_ids
