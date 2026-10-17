"""Query performance for the reels endpoints. `ReelCardSerializer` used to
resolve is_liked_by_me/is_saved_by_me with a `.filter().exists()` per reel,
so a 20-reel page of ItemStandardPagination cost ~40 extra round trips — on
the feed, on every per-store grid (`?store=<id>`) and on `saved/`.
`ReelViewSet.annotate_viewer_flags` now resolves both with Exists()
subqueries that ride inside the page's own SELECT.

These tests prove the *shape* of that, the same way
test_admin_query_performance does: the cost with 3 reels should equal the
cost with 15, not scale with N. Pinning an exact number instead would go
green again the day someone reintroduces the per-row lookup and updates the
number to match.

What is measured is the number of queries touching core_reellike /
core_savedreel, not the page's whole query count — the page total is *not*
flat yet, and TestTheWholePageIsFlat below says why.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from core.models import Reel, ReelLike, SavedReel, User, UserStore

pytestmark = pytest.mark.django_db

# The two tables the flags are read from. The annotated page mentions both
# inside one SELECT; the old code mentioned them once per reel.
FLAG_TABLES = ('core_reellike', 'core_savedreel')


def make_user(username):
    return User.objects.create_user(username=username, password='pass12345')


def make_store(username, name='متجر'):
    return UserStore.objects.create(user=make_user(username), name=name,
                                    is_active=True)


def make_reels(store, n, viewer=None, start=0):
    """n published reels. The viewer likes and saves every one of them, so
    the flags resolve to True — all-False is the one case a broken
    annotation could still get right by accident."""
    for i in range(start, start + n):
        reel = Reel.objects.create(
            store=store, caption=f'reel{i}', status='published',
            video_key=f'reels/{store.id}/{i}/video.mp4',
            thumbnail_key=f'reels/{store.id}/{i}/thumb.jpg',
            duration_seconds=20,
        )
        if viewer is not None:
            ReelLike.objects.create(reel=reel, user=viewer)
            SavedReel.objects.create(reel=reel, user=viewer)


def _capture(client, url, params, expected_rows):
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url, params)
        assert response.status_code == 200, response.data
        # Without this an endpoint that quietly returned nothing would also
        # hold the count flat, and the test would pass for the wrong reason.
        assert len(response.data['results']) == expected_rows, response.data
    return [q['sql'].lower() for q in ctx.captured_queries]


def _flag_queries(client, url, params, expected_rows):
    return [sql for sql in _capture(client, url, params, expected_rows)
            if any(table in sql for table in FLAG_TABLES)]


@pytest.fixture
def viewer():
    return make_user('mushahid')


@pytest.fixture
def client(viewer):
    client = APIClient()
    client.force_authenticate(user=viewer)
    return client


@pytest.fixture
def store():
    return make_store('tajir', 'متجر علي')


class TestTheFlagsCostTheSameAtAnyPageSize:
    """3 reels and 15 reels fit the same page (page_size=20), so any
    difference between the two runs is per-row work."""

    def test_the_feed(self, client, store, viewer):
        make_reels(store, 3, viewer=viewer)
        small = len(_flag_queries(client, '/api/reels/', {}, 3))

        make_reels(store, 12, viewer=viewer, start=3)
        large = len(_flag_queries(client, '/api/reels/', {}, 15))

        assert small == large == 1, \
            f'like/save lookups scaled with the page (N+1): {small} -> {large}'

    def test_the_per_store_grid(self, client, store, viewer):
        params = {'store': store.id}
        make_reels(store, 3, viewer=viewer)
        small = len(_flag_queries(client, '/api/reels/', params, 3))

        make_reels(store, 12, viewer=viewer, start=3)
        large = len(_flag_queries(client, '/api/reels/', params, 15))

        assert small == large == 1, \
            f'like/save lookups scaled with the page (N+1): {small} -> {large}'

    def test_saved_reels(self, client, store, viewer):
        # saved/ builds its own queryset (id__in, with the ordering
        # re-applied in Python), so it has to be annotated by hand — this is
        # the test that catches that being forgotten. Two queries here, not
        # one: the SavedReel id list the page is built from, then the
        # annotated page itself.
        make_reels(store, 3, viewer=viewer)
        small = len(_flag_queries(client, '/api/reels/saved/', {}, 3))

        make_reels(store, 12, viewer=viewer, start=3)
        large = len(_flag_queries(client, '/api/reels/saved/', {}, 15))

        assert small == large == 2, \
            f'like/save lookups scaled with the page (N+1): {small} -> {large}'


@pytest.mark.xfail(strict=True, reason=(
    'A second, unrelated N+1 still scales with the page: the nested store '
    'card is re-serialized per reel, and PublicUserStoreSerializer.'
    'get_total_active_items runs one Item.objects.count() each time. It '
    'cannot be annotated onto the Reel queryset — the serializer that needs '
    'the number receives a UserStore, not a Reel — so it is its own fix. '
    'Delete this marker when that lands.'))
class TestTheWholePageIsFlat:
    def test_the_feed(self, client, store, viewer):
        make_reels(store, 3, viewer=viewer)
        small = len(_capture(client, '/api/reels/', {}, 3))

        make_reels(store, 12, viewer=viewer, start=3)
        large = len(_capture(client, '/api/reels/', {}, 15))

        assert large == small, \
            f'query count grew with the page (N+1): {small} -> {large}'


class TestTheFlagsStillSayTheTruth:
    """Flat is worthless if it is flatly wrong — the annotation has to agree
    with the rows in core_reellike / core_savedreel, reel by reel."""

    def test_liked_and_saved_are_per_reel_not_per_page(self, client, store, viewer):
        make_reels(store, 3, viewer=viewer)          # liked + saved
        make_reels(store, 2, viewer=None, start=3)   # neither
        mixed = Reel.objects.order_by('id').last()
        ReelLike.objects.create(reel=mixed, user=viewer)  # liked, not saved

        by_id = {r['id']: r for r in client.get('/api/reels/').data['results']}

        for reel in Reel.objects.order_by('id')[:3]:
            assert by_id[reel.id]['is_liked_by_me'] is True
            assert by_id[reel.id]['is_saved_by_me'] is True
        assert by_id[mixed.id]['is_liked_by_me'] is True
        assert by_id[mixed.id]['is_saved_by_me'] is False

    def test_another_viewers_like_is_not_mine(self, client, store):
        # OuterRef('pk') pairs the subquery to the reel; the user_id filter
        # pairs it to the viewer. Drop the second and every reel anyone had
        # ever liked would come back liked.
        other = make_user('ghayri')
        make_reels(store, 2, viewer=other)

        for card in client.get('/api/reels/').data['results']:
            assert card['is_liked_by_me'] is False
            assert card['is_saved_by_me'] is False


class TestAGuestPaysNothingForFlagsTheyCannotHave:
    def test_the_flags_are_false(self, store, viewer):
        make_reels(store, 3, viewer=viewer)
        for card in APIClient().get('/api/reels/').data['results']:
            assert card['is_liked_by_me'] is False
            assert card['is_saved_by_me'] is False

    def test_resolving_them_costs_no_query(self, store, viewer):
        """A guest can neither like nor save, so both flags are False
        without asking the database: the viewset skips the annotation for
        anonymous requests, and the serializer short-circuits on
        request.user before reaching its per-row fallback.
        """
        make_reels(store, 3, viewer=viewer)
        queries = _capture(APIClient(), '/api/reels/', {}, 3)

        # Read as the constraint itself: the two tables are simply absent
        # from the guest's SQL. The row-count assertion inside _capture
        # already rules out the way this could pass for the wrong reason —
        # an endpoint that errored into an empty page would also touch
        # neither table.
        touched = [sql for sql in queries
                   if any(table in sql for table in FLAG_TABLES)]
        assert not touched, \
            f'a guest page queried the like/save tables: {touched}'
