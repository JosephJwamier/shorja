import base64
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import (
    AuctionListing, Bid, Category, FixedPriceListing, Item, LiveSession,
    LiveSessionComment, LiveSessionItem, LiveSessionLike, Location, Reservation,
    User, get_or_create_wallet,
)
from core.models import UserStore
from core.tasks import finalize_live_auction

pytestmark = pytest.mark.django_db

# Smallest possible valid PNG (1x1 transparent pixel) — DRF's ImageField
# validates uploads with Pillow, so arbitrary bytes are rejected.
_PNG_1X1 = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_store(user, name='Store'):
    return UserStore.objects.create(user=user, name=name, is_active=True)


def make_category(name='cat'):
    return Category.objects.create(name=name, slug=name)


def make_item(owner, category, quantity=5, status='active'):
    return Item.objects.create(
        owner=owner, title='Test Item', description='desc',
        category=category, condition='good', quantity=quantity, status=status,
    )


def make_listed_item(owner, category, price='200.00', deposit_percentage='10.00', quantity=5):
    item = make_item(owner, category, quantity=quantity)
    FixedPriceListing.objects.create(
        item=item, price=Decimal(price), deposit_percentage=Decimal(deposit_percentage),
    )
    return item


def fund_wallet(user, amount):
    wallet = get_or_create_wallet(user)
    wallet.balance = Decimal(str(amount))
    wallet.save(update_fields=['balance'])
    return wallet


def make_screenshot():
    return SimpleUploadedFile('screenshot.png', _PNG_1X1, content_type='image/png')


def make_session(store, status='live'):
    from django.utils import timezone
    return LiveSession.objects.create(
        store=store, title='My Live Show', status=status,
        started_at=timezone.now() if status == 'live' else None,
    )


def make_queue_item(session, item, price='100.00', deposit='20.00', status='now_showing'):
    from django.utils import timezone
    return LiveSessionItem.objects.create(
        session=session, item=item,
        price=Decimal(price), deposit_amount=Decimal(deposit),
        status=status, shown_at=timezone.now() if status == 'now_showing' else None,
    )


@pytest.fixture(autouse=True)
def locmem_cache(settings):
    settings.CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def owner():
    return make_user('shop_owner')


@pytest.fixture
def store(owner):
    return make_store(owner)


@pytest.fixture
def category():
    return make_category()


@pytest.fixture
def buyer():
    user = make_user('buyer')
    Location.objects.create(
        user=user, name='home', address_line1='x', city='Baghdad', state='بغداد',
        country='Iraq', contact_person='p', contact_phone='1',
    )
    return user


def auth(client, user):
    client.force_authenticate(user=user)
    return client


@patch('core.tasks.notify_store_followers_live.delay')
def test_go_live_transitions_and_rejects_double_go_live(mock_delay, client, owner, store):
    session = make_session(store, status='scheduled')
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/go_live/')
    assert response.status_code == 200
    session.refresh_from_db()
    assert session.status == 'live'
    assert session.started_at is not None
    mock_delay.assert_called_once_with(session.id)

    response = client.post(f'/api/live-sessions/{session.id}/go_live/')
    assert response.status_code == 400


@patch('core.tasks.notify_store_followers_live.delay')
def test_create_rejects_second_live_session_when_one_already_live(mock_delay, client, owner, store):
    make_session(store, status='live')
    auth(client, owner)

    response = client.post('/api/live-sessions/', {
        'title': 'Another show', 'start_immediately': True,
    })

    assert response.status_code == 400
    assert response.data['data']['status'] == 'live'


def test_queue_items_rejects_item_owner_mismatch(client, owner, store, category, buyer):
    session = make_session(store, status='live')
    other_item = make_item(buyer, category)  # owned by someone else, not the store owner
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/queue-items/', {
        'item_id': other_item.id, 'price': '50.00', 'deposit_amount': '10.00',
    })

    assert response.status_code == 403
    assert not LiveSessionItem.objects.filter(session=session, item=other_item).exists()


def test_now_showing_demotes_previous_entry(client, owner, store, category):
    session = make_session(store, status='live')
    item_a = make_item(owner, category)
    item_b = make_item(owner, category)
    entry_a = make_queue_item(session, item_a, status='now_showing')
    entry_b = LiveSessionItem.objects.create(
        session=session, item=item_b, price=Decimal('80.00'),
        deposit_amount=Decimal('15.00'), status='queued',
    )
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/now-showing/', {
        'queue_item_id': entry_b.id,
    })

    assert response.status_code == 200
    entry_a.refresh_from_db()
    entry_b.refresh_from_db()
    assert entry_a.status == 'queued'
    assert entry_b.status == 'now_showing'


def test_reserve_happy_path_debits_wallet_and_decrements_stock(client, owner, store, category, buyer):
    session = make_session(store, status='live')
    item = make_item(owner, category, quantity=5)
    entry = make_queue_item(session, item, price='100.00', deposit='20.00')
    fund_wallet(buyer, '1000.00')
    auth(client, buyer)

    response = client.post(
        f'/api/live-sessions/{session.id}/reserve/',
        {'queue_item_id': entry.id, 'quantity': 1, 'screenshot': make_screenshot()},
        format='multipart',
    )

    assert response.status_code == 201, response.data
    item.refresh_from_db()
    assert item.quantity == 4

    reservation = Reservation.objects.get(buyer=buyer, item=item)
    assert reservation.status == 'completed'
    assert reservation.is_deposit_only is True
    assert reservation.deposit_amount == Decimal('20.00')
    assert reservation.live_session_id == session.id
    assert reservation.live_session_item_id == entry.id
    assert reservation.proof_screenshot

    wallet = get_or_create_wallet(buyer)
    assert wallet.balance == Decimal('980.00')


def test_reserve_rejects_when_buyer_has_no_delivery_address(client, owner, store, category):
    buyer_without_address = make_user('buyer_no_address')
    session = make_session(store, status='live')
    item = make_item(owner, category, quantity=5)
    entry = make_queue_item(session, item, price='100.00', deposit='20.00')
    fund_wallet(buyer_without_address, '1000.00')
    auth(client, buyer_without_address)

    response = client.post(
        f'/api/live-sessions/{session.id}/reserve/',
        {'queue_item_id': entry.id, 'quantity': 1, 'screenshot': make_screenshot()},
        format='multipart',
    )

    assert response.status_code == 400
    item.refresh_from_db()
    assert item.quantity == 5
    assert not Reservation.objects.filter(item=item).exists()


def test_reserve_rejects_item_not_currently_showing(client, owner, store, category, buyer):
    session = make_session(store, status='live')
    item = make_item(owner, category)
    entry = make_queue_item(session, item, status='queued')
    fund_wallet(buyer, '1000.00')
    auth(client, buyer)

    response = client.post(
        f'/api/live-sessions/{session.id}/reserve/',
        {'queue_item_id': entry.id, 'quantity': 1, 'screenshot': make_screenshot()},
        format='multipart',
    )

    assert response.status_code == 400
    assert not Reservation.objects.filter(item=item).exists()


def test_reserve_rejects_insufficient_balance(client, owner, store, category, buyer):
    session = make_session(store, status='live')
    item = make_item(owner, category, quantity=5)
    entry = make_queue_item(session, item, price='100.00', deposit='20.00')
    # buyer wallet left at zero balance
    auth(client, buyer)

    response = client.post(
        f'/api/live-sessions/{session.id}/reserve/',
        {'queue_item_id': entry.id, 'quantity': 1, 'screenshot': make_screenshot()},
        format='multipart',
    )

    assert response.status_code == 400
    item.refresh_from_db()
    assert item.quantity == 5
    assert not Reservation.objects.filter(item=item).exists()


def test_reserve_rejects_reserving_own_item(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_item(owner, category)
    entry = make_queue_item(session, item)
    fund_wallet(owner, '1000.00')
    auth(client, owner)

    response = client.post(
        f'/api/live-sessions/{session.id}/reserve/',
        {'queue_item_id': entry.id, 'quantity': 1, 'screenshot': make_screenshot()},
        format='multipart',
    )

    assert response.status_code == 403


def test_reserve_marks_queue_item_sold_out_when_stock_hits_zero(client, owner, store, category, buyer):
    session = make_session(store, status='live')
    item = make_item(owner, category, quantity=1)
    entry = make_queue_item(session, item, price='100.00', deposit='20.00')
    fund_wallet(buyer, '1000.00')
    auth(client, buyer)

    response = client.post(
        f'/api/live-sessions/{session.id}/reserve/',
        {'queue_item_id': entry.id, 'quantity': 1, 'screenshot': make_screenshot()},
        format='multipart',
    )

    assert response.status_code == 201
    entry.refresh_from_db()
    assert entry.status == 'sold_out'


def test_cancel_reopens_sold_out_queue_item(client, owner, store, category, buyer):
    session = make_session(store, status='live')
    item = make_item(owner, category, quantity=1)
    entry = make_queue_item(session, item, price='100.00', deposit='20.00')
    fund_wallet(buyer, '1000.00')
    auth(client, buyer)

    reserve_response = client.post(
        f'/api/live-sessions/{session.id}/reserve/',
        {'queue_item_id': entry.id, 'quantity': 1, 'screenshot': make_screenshot()},
        format='multipart',
    )
    reservation_id = reserve_response.data['data']['id']
    entry.refresh_from_db()
    assert entry.status == 'sold_out'

    cancel_response = client.post(f'/api/reservations/{reservation_id}/cancel/')

    assert cancel_response.status_code == 200
    item.refresh_from_db()
    entry.refresh_from_db()
    assert item.quantity == 1
    assert entry.status == 'queued'


def test_end_session_skips_leftover_queue_items(client, owner, store, category):
    session = make_session(store, status='live')
    item_a = make_item(owner, category)
    item_b = make_item(owner, category)
    entry_a = make_queue_item(session, item_a, status='now_showing')
    entry_b = LiveSessionItem.objects.create(
        session=session, item=item_b, price=Decimal('80.00'),
        deposit_amount=Decimal('15.00'), status='queued',
    )
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/end-session/')

    assert response.status_code == 200
    session.refresh_from_db()
    entry_a.refresh_from_db()
    entry_b.refresh_from_db()
    assert session.status == 'ended'
    assert entry_a.status == 'skipped'
    assert entry_b.status == 'skipped'


def test_like_and_unlike_do_not_double_count(client, owner, store, buyer):
    session = make_session(store, status='live')
    auth(client, buyer)

    first = client.post(f'/api/live-sessions/{session.id}/like/')
    second = client.post(f'/api/live-sessions/{session.id}/like/')

    assert first.status_code == 200
    assert second.status_code == 200
    session.refresh_from_db()
    assert session.likes_count == 1
    assert LiveSessionLike.objects.filter(session=session, user=buyer).count() == 1

    unlike_once = client.post(f'/api/live-sessions/{session.id}/unlike/')
    unlike_twice = client.post(f'/api/live-sessions/{session.id}/unlike/')

    assert unlike_once.status_code == 200
    assert unlike_twice.status_code == 200
    session.refresh_from_db()
    assert session.likes_count == 0


def test_queue_item_detail_delete_removes_queued_entry(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_item(owner, category)
    entry = make_queue_item(session, item, status='queued')
    auth(client, owner)

    response = client.delete(f'/api/live-sessions/{session.id}/queue-items/{entry.id}/')

    assert response.status_code == 200
    assert not LiveSessionItem.objects.filter(id=entry.id).exists()


def test_queue_item_detail_delete_rejects_now_showing_entry(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_item(owner, category)
    entry = make_queue_item(session, item, status='now_showing')
    auth(client, owner)

    response = client.delete(f'/api/live-sessions/{session.id}/queue-items/{entry.id}/')

    assert response.status_code == 400
    assert LiveSessionItem.objects.filter(id=entry.id).exists()


def test_show_item_uses_listed_price_automatically(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_listed_item(owner, category, price='200.00', deposit_percentage='10.00')
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/show-item/', {'item_id': item.id})

    assert response.status_code == 200, response.data
    queue_item = LiveSessionItem.objects.get(session=session, item=item)
    assert queue_item.status == 'now_showing'
    assert queue_item.price == Decimal('200.00')
    assert queue_item.deposit_amount == Decimal('20.00')


def test_show_item_rejects_unlisted_item_without_override(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_item(owner, category)  # no FixedPriceListing
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/show-item/', {'item_id': item.id})

    assert response.status_code == 400
    assert not LiveSessionItem.objects.filter(session=session, item=item).exists()


def test_show_item_accepts_manual_price_override_for_unlisted_item(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_item(owner, category)
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/show-item/', {
        'item_id': item.id, 'price': '50.00', 'deposit_amount': '10.00',
    })

    assert response.status_code == 200
    queue_item = LiveSessionItem.objects.get(session=session, item=item)
    assert queue_item.price == Decimal('50.00')


def test_show_item_rejects_non_owner(client, owner, store, category, buyer):
    session = make_session(store, status='live')
    other_item = make_listed_item(buyer, category)
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/show-item/', {'item_id': other_item.id})

    assert response.status_code == 403


def test_show_item_reuses_existing_entry_instead_of_duplicating(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_listed_item(owner, category)
    auth(client, owner)

    first = client.post(f'/api/live-sessions/{session.id}/show-item/', {'item_id': item.id})
    second = client.post(f'/api/live-sessions/{session.id}/show-item/', {'item_id': item.id})

    assert first.status_code == 200
    assert second.status_code == 200
    assert LiveSessionItem.objects.filter(session=session, item=item).count() == 1


def test_catalog_lists_owner_items_with_suggested_price_and_session_status(client, owner, store, category):
    session = make_session(store, status='live')
    listed_item = make_listed_item(owner, category, price='150.00', deposit_percentage='10.00')
    entry = make_queue_item(session, listed_item, price='150.00', deposit='15.00')
    unlisted_item = make_item(owner, category)
    auth(client, owner)

    response = client.get(f'/api/live-sessions/{session.id}/catalog/')

    assert response.status_code == 200
    by_id = {row['item_id']: row for row in response.data['data']}
    assert by_id[listed_item.id]['suggested_price'] == '150.00'
    assert by_id[listed_item.id]['session_status'] == 'now_showing'
    assert by_id[listed_item.id]['queue_item_id'] == entry.id
    assert by_id[unlisted_item.id]['suggested_price'] is None
    assert by_id[unlisted_item.id]['session_status'] == 'not_shown'


def test_post_comment_broadcasts_and_appears_in_history(client, owner, store, buyer):
    session = make_session(store, status='live')
    auth(client, buyer)

    post_response = client.post(f'/api/live-sessions/{session.id}/comments/', {'content': 'كم السعر؟'})
    assert post_response.status_code == 201

    anon_client = APIClient()
    list_response = anon_client.get(f'/api/live-sessions/{session.id}/comments/')

    assert list_response.status_code == 200
    contents = [c['content'] for c in list_response.data['data']]
    assert 'كم السعر؟' in contents


def test_comment_rejected_when_session_not_live(client, owner, store, buyer):
    session = make_session(store, status='scheduled')
    auth(client, buyer)

    response = client.post(f'/api/live-sessions/{session.id}/comments/', {'content': 'hello'})

    assert response.status_code == 400


def test_comment_deleted_by_author_or_store_owner(client, owner, store, buyer):
    session = make_session(store, status='live')
    auth(client, buyer)
    comment = LiveSessionComment.objects.create(session=session, user=buyer, content='test comment')

    response = client.delete(f'/api/live-sessions/{session.id}/comments/{comment.id}/')

    assert response.status_code == 200
    assert not LiveSessionComment.objects.filter(id=comment.id).exists()

    comment2 = LiveSessionComment.objects.create(session=session, user=buyer, content='another one')
    auth(client, owner)
    response2 = client.delete(f'/api/live-sessions/{session.id}/comments/{comment2.id}/')

    assert response2.status_code == 200
    assert not LiveSessionComment.objects.filter(id=comment2.id).exists()


def test_comment_delete_rejected_for_unrelated_user(client, owner, store, buyer):
    session = make_session(store, status='live')
    comment = LiveSessionComment.objects.create(session=session, user=buyer, content='test comment')
    stranger = make_user('stranger')
    auth(client, stranger)

    response = client.delete(f'/api/live-sessions/{session.id}/comments/{comment.id}/')

    assert response.status_code == 403
    assert LiveSessionComment.objects.filter(id=comment.id).exists()


def test_quick_show_creates_item_never_in_catalog_and_shows_it(client, owner, store):
    session = make_session(store, status='live')
    items_before = Item.objects.count()
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/quick-show/', {
        'title': 'قميص أزرق مقاس L', 'price': '35.00', 'deposit_amount': '5.00',
    })

    assert response.status_code == 201, response.data
    assert Item.objects.count() == items_before + 1

    queue_item = LiveSessionItem.objects.get(id=response.data['data']['id'])
    assert queue_item.status == 'now_showing'
    assert queue_item.price == Decimal('35.00')
    assert queue_item.deposit_amount == Decimal('5.00')
    assert queue_item.item.owner == owner
    assert queue_item.item.title == 'قميص أزرق مقاس L'
    assert queue_item.item.quantity == 1  # default


def test_quick_show_rejects_when_not_live(client, owner, store):
    session = make_session(store, status='scheduled')
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/quick-show/', {
        'title': 'anything', 'price': '10.00', 'deposit_amount': '2.00',
    })

    assert response.status_code == 400


def test_quick_show_defaults_to_shared_live_category_across_calls(client, owner, store):
    session = make_session(store, status='live')
    auth(client, owner)

    client.post(f'/api/live-sessions/{session.id}/quick-show/', {
        'title': 'item one', 'price': '10.00', 'deposit_amount': '1.00',
    })
    client.post(f'/api/live-sessions/{session.id}/quick-show/', {
        'title': 'item two', 'price': '20.00', 'deposit_amount': '2.00',
    })

    categories = Category.objects.filter(slug='live-quick-show')
    assert categories.count() == 1
    item_one = Item.objects.get(title='item one')
    item_two = Item.objects.get(title='item two')
    assert item_one.category_id == categories.first().id == item_two.category_id


def test_quick_show_rejects_deposit_greater_than_price(client, owner, store):
    session = make_session(store, status='live')
    auth(client, owner)

    response = client.post(f'/api/live-sessions/{session.id}/quick-show/', {
        'title': 'item', 'price': '10.00', 'deposit_amount': '20.00',
    })

    assert response.status_code == 400


def test_quick_show_then_reserve_full_end_to_end_flow(client, owner, store, buyer):
    """The exact scenario the seller asked for: a product that was never in
    the app, shown live, and a viewer reserves it with a deposit + screenshot."""
    session = make_session(store, status='live')
    fund_wallet(buyer, '1000.00')
    auth(client, owner)

    show_response = client.post(f'/api/live-sessions/{session.id}/quick-show/', {
        'title': 'ساعة يد', 'price': '80.00', 'deposit_amount': '15.00',
    })
    queue_item_id = show_response.data['data']['id']
    new_item_id = show_response.data['data']['item']['id']

    auth(client, buyer)
    reserve_response = client.post(
        f'/api/live-sessions/{session.id}/reserve/',
        {'queue_item_id': queue_item_id, 'quantity': 1, 'screenshot': make_screenshot()},
        format='multipart',
    )

    assert reserve_response.status_code == 201, reserve_response.data
    reservation = Reservation.objects.get(id=reserve_response.data['data']['id'])
    assert reservation.item_id == new_item_id
    assert reservation.buyer == buyer
    assert reservation.deposit_amount == Decimal('15.00')
    assert reservation.proof_screenshot

    wallet = get_or_create_wallet(buyer)
    assert wallet.balance == Decimal('985.00')


def _quick_auction_payload(title='مزاد سريع', starting_price='50.00', minimum_increment='5.00', bid_deposit='10.00'):
    return {
        'title': title, 'starting_price': starting_price,
        'minimum_increment': minimum_increment, 'bid_deposit': bid_deposit,
    }


def test_quick_auction_starts_two_minute_auction(client, owner, store):
    session = make_session(store, status='live')
    auth(client, owner)

    with patch('core.tasks.finalize_live_auction.apply_async') as mock_apply_async:
        response = client.post(
            f'/api/live-sessions/{session.id}/quick-auction/', _quick_auction_payload()
        )

    assert response.status_code == 201, response.data
    auction = AuctionListing.objects.get(id=response.data['data']['id'])
    assert auction.status == 'active'
    assert auction.live_session_id == session.id
    assert auction.starting_price == Decimal('50.00')
    assert auction.current_price == Decimal('50.00')
    assert auction.minimum_increment == Decimal('5.00')
    assert auction.bid_deposit == Decimal('10.00')
    assert (auction.end_time - auction.start_time) == timedelta(minutes=2)

    mock_apply_async.assert_called_once_with(args=[auction.id], eta=auction.end_time)


def test_quick_auction_demotes_now_showing_item(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_item(owner, category)
    showing_entry = make_queue_item(session, item, status='now_showing')
    auth(client, owner)

    with patch('core.tasks.finalize_live_auction.apply_async'):
        response = client.post(
            f'/api/live-sessions/{session.id}/quick-auction/', _quick_auction_payload()
        )

    assert response.status_code == 201
    showing_entry.refresh_from_db()
    assert showing_entry.status == 'queued'


def test_quick_auction_rejects_second_active_auction(client, owner, store):
    session = make_session(store, status='live')
    auth(client, owner)

    with patch('core.tasks.finalize_live_auction.apply_async'):
        first = client.post(
            f'/api/live-sessions/{session.id}/quick-auction/', _quick_auction_payload(title='الأول')
        )
        second = client.post(
            f'/api/live-sessions/{session.id}/quick-auction/', _quick_auction_payload(title='الثاني')
        )

    assert first.status_code == 201
    assert second.status_code == 400


def test_quick_auction_rejects_when_not_live(client, owner, store):
    session = make_session(store, status='scheduled')
    auth(client, owner)

    response = client.post(
        f'/api/live-sessions/{session.id}/quick-auction/', _quick_auction_payload()
    )

    assert response.status_code == 400


def test_show_item_rejects_while_live_auction_active(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_listed_item(owner, category)
    auth(client, owner)

    with patch('core.tasks.finalize_live_auction.apply_async'):
        client.post(f'/api/live-sessions/{session.id}/quick-auction/', _quick_auction_payload())

    response = client.post(f'/api/live-sessions/{session.id}/show-item/', {'item_id': item.id})

    assert response.status_code == 400


def test_quick_show_rejects_while_live_auction_active(client, owner, store):
    session = make_session(store, status='live')
    auth(client, owner)

    with patch('core.tasks.finalize_live_auction.apply_async'):
        client.post(f'/api/live-sessions/{session.id}/quick-auction/', _quick_auction_payload())

    response = client.post(f'/api/live-sessions/{session.id}/quick-show/', {
        'title': 'منتج آخر', 'price': '40.00', 'deposit_amount': '5.00',
    })

    assert response.status_code == 400


def test_place_bid_on_live_auction_broadcasts_to_live_channel(client, owner, store, buyer):
    session = make_session(store, status='live')
    auth(client, owner)
    with patch('core.tasks.finalize_live_auction.apply_async'):
        create_response = client.post(
            f'/api/live-sessions/{session.id}/quick-auction/', _quick_auction_payload()
        )
    auction_id = create_response.data['data']['id']

    fund_wallet(buyer, '1000.00')
    auth(client, buyer)

    with patch('core.views.broadcast_live_event') as mock_broadcast:
        bid_response = client.post(
            f'/api/auctions/{auction_id}/place_bid/', {'amount': 55.00}, format='json'
        )

    assert bid_response.status_code == 201, bid_response.data
    auction = AuctionListing.objects.get(id=auction_id)
    assert auction.current_price == Decimal('55.00')

    live_events = [c.args[1] for c in mock_broadcast.call_args_list if c.args[0] == session.id]
    assert 'auction_bid' in live_events


def test_finalize_live_auction_settles_winner_into_reservation(client, owner, store, category, buyer):
    session = make_session(store, status='live')
    item = make_item(owner, category, quantity=1)
    auction = AuctionListing.objects.create(
        item=item, starting_price=Decimal('100.00'), current_price=Decimal('120.00'),
        minimum_increment=Decimal('10.00'), bid_deposit=Decimal('20.00'),
        start_time=timezone.now() - timedelta(minutes=3),
        end_time=timezone.now() - timedelta(minutes=1),
        status='active', live_session=session,
    )
    fund_wallet(buyer, '1000.00')
    Bid.objects.create(auction=auction, bidder=buyer, amount=Decimal('120.00'),
                        deposit_paid=True, is_winning=True)

    finalize_live_auction(auction.id)

    auction.refresh_from_db()
    assert auction.status == 'ended'
    assert auction.winner_id == buyer.id
    reservation = Reservation.objects.get(auction=auction)
    assert reservation.buyer == buyer
    assert reservation.status == 'pending'
    assert reservation.total_price == Decimal('120.00')
    assert reservation.deposit_amount == Decimal('20.00')


def test_finalize_live_auction_no_bids_marks_item_draft(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_item(owner, category, quantity=1)
    auction = AuctionListing.objects.create(
        item=item, starting_price=Decimal('100.00'), current_price=Decimal('100.00'),
        minimum_increment=Decimal('10.00'), bid_deposit=Decimal('20.00'),
        start_time=timezone.now() - timedelta(minutes=3),
        end_time=timezone.now() - timedelta(minutes=1),
        status='active', live_session=session,
    )

    finalize_live_auction(auction.id)

    auction.refresh_from_db()
    item.refresh_from_db()
    assert auction.status == 'ended'
    assert item.status == 'draft'
    assert not Reservation.objects.filter(auction=auction).exists()


def test_finalize_live_auction_skips_if_not_due_yet(client, owner, store, category):
    session = make_session(store, status='live')
    item = make_item(owner, category, quantity=1)
    auction = AuctionListing.objects.create(
        item=item, starting_price=Decimal('100.00'), current_price=Decimal('100.00'),
        minimum_increment=Decimal('10.00'), bid_deposit=Decimal('20.00'),
        start_time=timezone.now(),
        end_time=timezone.now() + timedelta(minutes=2),
        status='active', live_session=session,
    )

    finalize_live_auction(auction.id)

    auction.refresh_from_db()
    assert auction.status == 'active'
