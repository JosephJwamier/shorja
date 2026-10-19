"""Live media on our own SRS box: token math, the credentials the app gets,
the viewing policy, and the SRS -> Django hooks (exercised through their
real URL, since they are routed now).
"""
import pytest
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

from core import live_hooks
from core.models import LiveSession, LiveSessionBan, User, UserStore
from core.services.live_media import get_live_media_provider
from core.services.live_media.base import MediaProviderError
from core.services.live_media.srs import (
    ROLE_PUBLISHER, ROLE_SUBSCRIBER, make_stream_token, turn_credentials, verify_stream_token,
)

pytestmark = pytest.mark.django_db

SRS_SETTINGS = dict(
    LIVE_MEDIA_PROVIDER='srs',
    SRS_PUBLIC_BASE_URL='https://live.example.com',
    SRS_HOOK_SECRET='hook-secret',
    LIVE_TOKEN_SECRET='token-secret',
    LIVE_TOKEN_TTL_SECONDS=600,
    TURN_HOST='live.example.com',
    TURN_PORT=3478,
    TURN_TLS_PORT=5349,
    TURN_SECRET='turn-secret',
)


@pytest.fixture(autouse=True)
def srs_settings(settings):
    for k, v in SRS_SETTINGS.items():
        setattr(settings, k, v)


def make_session(username='seller', status='live'):
    user = User.objects.create_user(username=username, password='x')
    store = UserStore.objects.create(user=user, name='s')
    return LiveSession.objects.create(store=store, title='t', status=status), user


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def test_token_round_trips_role_stream_and_user():
    token = make_stream_token('live_abc', ROLE_SUBSCRIBER, 60, user_id=42, now=1000)
    assert verify_stream_token(token, stream_name='live_abc', role=ROLE_SUBSCRIBER, now=1010) == 42


def test_token_is_bound_to_its_stream_and_role():
    token = make_stream_token('live_abc', ROLE_SUBSCRIBER, 60, user_id=1, now=1000)
    assert verify_stream_token(token, stream_name='live_xyz', role=ROLE_SUBSCRIBER, now=1010) is None
    assert verify_stream_token(token, stream_name='live_abc', role=ROLE_PUBLISHER, now=1010) is None


def test_token_expires_and_rejects_tampering():
    token = make_stream_token('live_abc', ROLE_PUBLISHER, 60, user_id=1, now=1000)
    assert verify_stream_token(token, stream_name='live_abc', role=ROLE_PUBLISHER, now=1061) is None
    forged = token[:-1] + ('0' if token[-1] != '0' else '1')
    assert verify_stream_token(forged, stream_name='live_abc', role=ROLE_PUBLISHER, now=1010) is None
    assert verify_stream_token('garbage', stream_name='live_abc', role=ROLE_PUBLISHER) is None


def test_turn_credentials_follow_the_rest_api_shape():
    servers = turn_credentials('u7', 600, now=1000)
    assert servers[0]['username'] == '1600:u7'
    assert servers[0]['credential']


def test_turns_points_at_the_tls_listener_not_the_plain_one():
    # Same host, different port. Getting this wrong is invisible: the
    # client simply never gets a relay candidate and the viewer sees a
    # stream that will not start.
    urls = turn_credentials('u7', 600, now=1000)[0]['urls']
    assert 'turn:live.example.com:3478?transport=udp' in urls
    assert 'turn:live.example.com:3478?transport=tcp' in urls
    assert 'turns:live.example.com:5349?transport=tcp' in urls


def test_turn_is_omitted_when_not_configured(settings):
    settings.TURN_SECRET = ''
    assert turn_credentials('u7', 600) == []


# ---------------------------------------------------------------------------
# Credentials the app receives
# ---------------------------------------------------------------------------

def test_publish_and_play_credentials_shape():
    session, owner = make_session()
    provider = get_live_media_provider()
    pub = provider.publish_credentials(session, owner)
    play = provider.play_credentials(session, owner)
    stream = session.stream_name
    assert pub['provider'] == 'srs' and pub['role'] == 'publisher'
    assert pub['whip_url'].startswith('https://live.example.com/rtc/v1/whip/?app=live&stream=%s&token=' % stream)
    assert play['whep_url'].startswith('https://live.example.com/rtc/v1/whep/?app=live&stream=%s&token=' % stream)
    assert play['hls_url'].startswith('https://live.example.com/live/%s.m3u8' % stream)
    assert play['ice_servers'][0]['urls'] == ['stun:live.example.com:3478']
    assert play['ice_servers'][1]['username'].endswith(':u%d' % owner.id)
    # A play token must not be usable to publish.
    play_token = play['whep_url'].rsplit('token=', 1)[1]
    assert verify_stream_token(play_token, stream_name=stream, role=ROLE_PUBLISHER) is None


def test_provider_refuses_to_run_unconfigured(settings):
    settings.SRS_PUBLIC_BASE_URL = ''
    session, owner = make_session()
    with pytest.raises(MediaProviderError):
        get_live_media_provider().publish_credentials(session, owner)


def test_unknown_provider_name_raises_instead_of_falling_back(settings):
    # A typo in production must stop live media loudly, not quietly serve
    # credentials for a server nobody configured.
    settings.LIVE_MEDIA_PROVIDER = 'agora'
    with pytest.raises(MediaProviderError):
        get_live_media_provider()


# ---------------------------------------------------------------------------
# The join-token endpoint
# ---------------------------------------------------------------------------

def join_token(session, user=None, role='subscriber'):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client.post(
        reverse('live-session-join-token', args=[session.id]), {'role': role}, format='json',
    )


def test_viewer_gets_play_credentials_for_their_own_account():
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    response = join_token(session, viewer)
    assert response.status_code == 200
    data = response.data['data']
    assert data['provider'] == 'srs' and data['role'] == 'subscriber'
    assert data['stream_name'] == session.stream_name
    token = data['whep_url'].rsplit('token=', 1)[1]
    assert verify_stream_token(token, stream_name=session.stream_name,
                               role=ROLE_SUBSCRIBER) == viewer.id


def test_only_the_store_owner_can_get_publish_credentials():
    session, owner = make_session()
    intruder = User.objects.create_user(username='intruder', password='x')
    UserStore.objects.create(user=intruder, name='other')
    assert join_token(session, owner, role='publisher').status_code == 200
    assert join_token(session, intruder, role='publisher').status_code == 403


def test_watching_needs_an_account():
    # Every credential names its user, which is what makes bans and viewer
    # counts mean anything — so an anonymous caller gets no token at all.
    session, _ = make_session()
    assert join_token(session).status_code in (401, 403)


def test_a_bad_role_is_rejected():
    session, owner = make_session()
    assert join_token(session, owner, role='moderator').status_code == 400


def test_unconfigured_live_media_is_503_not_500(settings):
    settings.SRS_PUBLIC_BASE_URL = ''
    session, owner = make_session()
    response = join_token(session, owner)
    assert response.status_code == 503
    assert response.data['code'] == 'live_media_unavailable'


# ---------------------------------------------------------------------------
# Hooks (SRS -> Django), through the routed URL
# ---------------------------------------------------------------------------

def hook(event, stream, token='', secret='hook-secret'):
    url = '%s?secret=%s' % (reverse('srs-hook', args=[event]), secret)
    return APIClient().post(url, {'action': event, 'app': 'live', 'stream': stream,
                                  'param': '?token=%s' % token}, format='json')


def test_hook_without_the_shared_secret_is_refused():
    session, _ = make_session()
    response = hook('on_publish', session.stream_name, secret='wrong')
    assert response.status_code == 403


def test_publish_needs_a_publish_token_for_that_stream():
    session, owner = make_session()
    stream = session.stream_name
    good = make_stream_token(stream, ROLE_PUBLISHER, 60, user_id=owner.id)
    play = make_stream_token(stream, ROLE_SUBSCRIBER, 60, user_id=owner.id)
    other = make_stream_token('live_other', ROLE_PUBLISHER, 60, user_id=owner.id)
    assert hook('on_publish', stream, good).data == {'code': 0}
    assert hook('on_publish', stream, play).data == {'code': 1}
    assert hook('on_publish', stream, other).data == {'code': 1}
    assert hook('on_publish', stream, '').data == {'code': 1}


def test_a_seller_may_publish_before_going_live():
    # The camera opens while the session is still 'scheduled'; refusing
    # that would mean no way to check framing before an audience arrives.
    session, owner = make_session(status='scheduled')
    token = make_stream_token(session.stream_name, ROLE_PUBLISHER, 60, user_id=owner.id)
    assert hook('on_publish', session.stream_name, token).data == {'code': 0}


def test_publish_is_refused_once_the_session_ended():
    session, owner = make_session(status='ended')
    token = make_stream_token(session.stream_name, ROLE_PUBLISHER, 60, user_id=owner.id)
    assert hook('on_publish', session.stream_name, token).data == {'code': 1}


def test_unknown_stream_and_unknown_event_are_denied():
    session, _ = make_session()
    assert hook('on_publish', 'live_nope').data == {'code': 1}
    assert hook('on_whatever', session.stream_name).status_code == 404


# ---------------------------------------------------------------------------
# The viewing policy, through on_play
# ---------------------------------------------------------------------------

def play(session, user_id):
    token = make_stream_token(session.stream_name, ROLE_SUBSCRIBER, 60, user_id=user_id)
    return hook('on_play', session.stream_name, token).data


def test_an_ordinary_viewer_may_watch_a_live_session():
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert play(session, viewer.id) == {'code': 0}


def test_an_anonymous_token_may_not_watch():
    session, _ = make_session()
    assert play(session, 0) == {'code': 1}


def test_watching_stops_when_the_session_is_not_live():
    # A stream name outlives its session, so without this check a token
    # minted during the broadcast would keep replaying it afterwards.
    viewer = User.objects.create_user(username='viewer', password='x')
    for status in ('scheduled', 'ended', 'cancelled'):
        session, _ = make_session(username='seller_%s' % status, status=status)
        assert play(session, viewer.id) == {'code': 1}, status


def test_a_banned_viewer_is_cut_off_at_the_media_server():
    session, owner = make_session()
    banned = User.objects.create_user(username='banned', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    assert play(session, banned.id) == {'code': 1}


def test_play_with_a_bad_token_is_denied():
    session, _ = make_session()
    assert hook('on_play', session.stream_name, 'nope').data == {'code': 1}


# ---------------------------------------------------------------------------
# The same viewing policy on the HLS path (Caddy forward_auth -> HlsAuthView)
# ---------------------------------------------------------------------------
# SRS fires no hook for HLS, so until this endpoint existed the ?token= in
# hls_url was decorative: a banned viewer kept watching and a finished
# session kept serving. These are the same four refusals on_play applies,
# asked the way Caddy asks them.

def hls(uri, *, address='203.0.113.7', secret='hook-secret', method='GET'):
    """A forward_auth subrequest: Caddy rewrites the URI to this endpoint
    and passes the real one in X-Forwarded-Uri."""
    url = '%s?secret=%s' % (reverse('srs-hls-auth'), secret)
    return APIClient().get(url, HTTP_X_FORWARDED_URI=uri,
                           HTTP_X_FORWARDED_METHOD=method,
                           HTTP_X_FORWARDED_FOR=address)


def subscriber_token(session, user_id, stream=None):
    return make_stream_token(stream or session.stream_name, ROLE_SUBSCRIBER,
                             60, user_id=user_id)


def playlist(session, user_id=None, token=None):
    if token is None:
        token = subscriber_token(session, user_id)
    return '/live/%s.m3u8?token=%s' % (session.stream_name, token)


def segment(session, seq=7, token=None):
    return '/live/%s-%d.ts%s' % (session.stream_name, seq,
                                 '?token=%s' % token if token else '')


def test_a_viewer_of_a_live_session_is_allowed_the_playlist():
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(playlist(session, viewer.id)).status_code == 204


def test_a_banned_viewer_is_refused_the_playlist():
    # The one that matters most: the ban used to cut WebRTC only, and the
    # app's own fallback then moved the banned viewer onto HLS.
    session, owner = make_session()
    banned = User.objects.create_user(username='banned', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    assert hls(playlist(session, banned.id)).status_code == 403


def test_an_anonymous_token_is_refused_on_the_hls_path():
    session, _ = make_session()
    assert hls(playlist(session, 0)).status_code == 403


def test_a_token_for_another_stream_or_role_is_refused():
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    elsewhere = subscriber_token(session, viewer.id, stream='live_other')
    publish = make_stream_token(session.stream_name, ROLE_PUBLISHER, 60, user_id=viewer.id)
    assert hls(playlist(session, token=elsewhere)).status_code == 403
    assert hls(playlist(session, token=publish)).status_code == 403
    assert hls(playlist(session, token='garbage')).status_code == 403


def test_a_playlist_is_refused_once_the_session_is_not_live():
    # A stream name outlives its session and the publisher may still be up,
    # so this is what stops a finished broadcast from serving on.
    viewer = User.objects.create_user(username='viewer', password='x')
    for status in ('scheduled', 'ended', 'cancelled'):
        session, _ = make_session(username='seller_%s' % status, status=status)
        assert hls(playlist(session, viewer.id)).status_code == 403, status


def test_a_playlist_without_a_token_is_refused():
    # The app's own playlist URL always carries one; a bare URL is someone
    # who was handed it.
    session, _ = make_session()
    assert hls('/live/%s.m3u8' % session.stream_name).status_code == 403


def test_an_unknown_stream_is_refused_even_with_a_valid_signature():
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    token = make_stream_token('live_nosession', ROLE_SUBSCRIBER, 60, user_id=viewer.id)
    assert hls('/live/live_nosession.m3u8?token=%s' % token).status_code == 403


def test_the_endpoint_needs_the_shared_secret():
    # Without it this is an oracle for who is banned from which session,
    # answerable by anyone who finds the URL.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(playlist(session, viewer.id), secret='wrong').status_code == 403
    assert hls(playlist(session, viewer.id), secret='').status_code == 403


def test_nothing_but_a_playlist_or_a_segment_is_authorized():
    # A DVR recording under /live/ is an unbounded MP4 of the whole
    # broadcast; refusing it here is the right default while nothing
    # uploads or deletes those files.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    token = subscriber_token(session, viewer.id)
    assert hls('/live/dvr/%s.1700000000.mp4?token=%s'
               % (session.stream_name, token)).status_code == 403
    assert hls('/live/?token=%s' % token).status_code == 403


def test_the_original_request_must_be_a_read():
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(playlist(session, viewer.id), method='POST').status_code == 403


# -- segments, which arrive with no token ------------------------------------
# A live m3u8 names its segments relatively and no player copies the
# playlist's query string onto them, so the only evidence a segment request
# carries is where it came from.

def test_a_segment_rides_on_the_grant_left_by_the_playlist():
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(playlist(session, viewer.id)).status_code == 204
    assert hls(segment(session)).status_code == 204


def test_a_segment_from_an_address_that_never_fetched_the_playlist_is_refused():
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(segment(session)).status_code == 403
    assert hls(playlist(session, viewer.id), address='203.0.113.7').status_code == 204
    assert hls(segment(session), address='198.51.100.4').status_code == 403


def test_a_grant_does_not_cross_streams():
    other, _ = make_session(username='other_seller')
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(playlist(session, viewer.id)).status_code == 204
    assert hls(segment(other)).status_code == 403


def test_a_segment_does_not_renew_its_own_grant():
    # The grant exists only because a playlist fetch was allowed seconds
    # ago. If segments renewed it, a client could stop reloading the
    # playlist and never be re-checked — which is the whole mechanism.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(playlist(session, viewer.id)).status_code == 204
    key = live_hooks._hls_grant_key(session.stream_name, '203.0.113.7')
    assert cache.get(key)
    assert hls(segment(session)).status_code == 204
    cache.delete(key)                      # the grant's few seconds running out
    assert hls(segment(session)).status_code == 403
    assert cache.get(key) is None


def test_a_segment_that_does_carry_a_token_is_judged_on_it():
    # Forward compatibility, and it must not be a way around the policy:
    # the grant is ignored when there is better evidence.
    session, owner = make_session()
    banned = User.objects.create_user(username='banned', password='x')
    viewer = User.objects.create_user(username='viewer', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    assert hls(playlist(session, viewer.id)).status_code == 204
    assert hls(segment(session, token=subscriber_token(session, viewer.id))).status_code == 204
    assert hls(segment(session, token=subscriber_token(session, banned.id))).status_code == 403


def test_a_forged_forwarded_for_cannot_borrow_a_grant():
    # Caddy replaces the header with the real peer, but if it ever appended
    # instead, the entry the client sent itself must not be the one that
    # counts — otherwise naming a watching neighbour's address is enough.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(playlist(session, viewer.id), address='203.0.113.7').status_code == 204
    assert hls(segment(session), address='203.0.113.7, 198.51.100.9').status_code == 403


# -- what it costs -----------------------------------------------------------

def test_the_policy_is_not_re_queried_for_every_request(django_assert_num_queries):
    # Hit once per playlist reload and once per two-second segment, so a
    # ban query per request is not an option: ~100/s at a hundred viewers.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    assert hls(playlist(session, viewer.id)).status_code == 204
    with django_assert_num_queries(0):
        assert hls(playlist(session, viewer.id)).status_code == 204
        assert hls(segment(session)).status_code == 204
        assert hls(segment(session, seq=8)).status_code == 204


def test_a_refusal_is_cached_as_well_as_an_allow(django_assert_num_queries):
    # A refused player keeps retrying, which would otherwise make the
    # banned viewer the cheapest query generator in the system.
    session, owner = make_session()
    banned = User.objects.create_user(username='banned', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    assert hls(playlist(session, banned.id)).status_code == 403
    with django_assert_num_queries(0):
        assert hls(playlist(session, banned.id)).status_code == 403


def test_the_cached_decision_is_per_viewer_not_per_stream():
    session, owner = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    banned = User.objects.create_user(username='banned', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    assert hls(playlist(session, viewer.id)).status_code == 204
    assert hls(playlist(session, banned.id)).status_code == 403
    assert hls(playlist(session, viewer.id)).status_code == 204
