"""Live media on our own SRS box: token math, the credentials the app gets,
the viewing policy, and the SRS -> Django hooks (exercised through their
real URL, since they are routed now).
"""
import time
from urllib.parse import parse_qs

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


@pytest.mark.parametrize('configured', ['', None])
def test_an_unconfigured_hook_secret_refuses_everyone(settings, configured):
    """الراية الفارغة كانت تفتح الخطّاف للعالم.

    `SRS_HOOK_SECRET` قيمتها الافتراضية `''` في الإعدادات، والمقارنة كانت
    `request.query_params.get("secret", "") != settings.SRS_HOOK_SECRET` —
    فنداءٌ بلا سرّ أصلاً يقارن `''` بـ`''` **فينجح**. أي أن أول طبقة من
    طبقتي الحماية تتعطّل صامتةً على كل نشرٍ نُسي فيه المتغيّر، وهو كل نشرٍ
    جديد لأن `.env.production.example` يشحنه فارغاً.
    """
    settings.SRS_HOOK_SECRET = configured
    session, _ = make_session()
    # لا سرّ في الطلب ولا في الإعدادات: الحالة التي كانت تمرّ.
    assert hook('on_publish', session.stream_name, secret='').status_code == 403
    assert hook('on_play', session.stream_name, secret='').status_code == 403


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
# The same viewing policy on the HLS path
# ---------------------------------------------------------------------------
# SRS fires no hook for HLS, so until these endpoints existed the ?token= in
# hls_url was decorative: a banned viewer kept watching and a finished
# session kept serving. Django now writes the playlist — applying the policy
# and signing every URI in it for one viewer — and Caddy sends each segment
# request to HlsAuthView, which reads that signature back out of the query
# string. See the comment above HlsPlaylistView for why neither a request
# header nor a cookie nor the client's address can carry it instead.

ORIGIN_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-ALLOW-CACHE:NO
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:12
#EXTINF:2.0, no desc
%(stream)s-12.ts
#EXTINF:2.0, no desc
%(stream)s-13.ts
"""


class FakeOrigin:
    """Stands in for SRS's http_server on the loopback."""

    def __init__(self):
        self.calls = []
        self.status = 200
        self.body = ''
        self.unreachable = False

    def serving(self, stream):
        self.status, self.body = 200, ORIGIN_PLAYLIST % {'stream': stream}
        return self

    def get(self, url, timeout=None):
        self.calls.append(url)
        if self.unreachable:
            raise live_hooks.requests.RequestException('connection refused')
        return type('Resp', (), {'status_code': self.status, 'text': self.body})()


@pytest.fixture
def origin(monkeypatch):
    fake = FakeOrigin()
    monkeypatch.setattr(live_hooks.requests, 'get', fake.get)
    return fake


def tok(session, user_id, stream=None, ttl=60):
    return make_stream_token(stream or session.stream_name, ROLE_SUBSCRIBER,
                             ttl, user_id=user_id)


def ask_playlist(session, token=None, *, secret='hook-secret', uri=None):
    """What Caddy sends for a `.m3u8`: the path rewritten to this view, the
    original URI in X-Forwarded-Uri."""
    url = '%s?secret=%s' % (reverse('srs-hls-playlist'), secret)
    if uri is None:
        uri = '/live/%s.m3u8' % session.stream_name
        if token:
            uri += '?token=%s' % token
    return APIClient().get(url, HTTP_X_FORWARDED_URI=uri)


def ask_segment(uri, *, secret='hook-secret', method='GET'):
    """What Caddy's forward_auth sends for a `.ts`."""
    url = '%s?secret=%s' % (reverse('srs-hls-auth'), secret)
    return APIClient().get(url, HTTP_X_FORWARDED_URI=uri,
                           HTTP_X_FORWARDED_METHOD=method)


def segment_lines(body):
    return [l for l in body.splitlines() if l and not l.startswith('#')]


# -- the playlist ------------------------------------------------------------

def test_a_viewer_of_a_live_session_gets_a_playlist_signed_for_them(origin):
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)

    response = ask_playlist(session, tok(session, viewer.id))
    assert response.status_code == 200
    assert response['Content-Type'] == 'application/vnd.apple.mpegurl'
    body = response.content.decode()

    lines = segment_lines(body)
    assert len(lines) == 2
    for line in lines:
        name, _, query = line.partition('?')
        assert name.endswith('.ts')
        token = parse_qs(query)['token'][0]
        # Signed for this stream and this viewer, and nobody else's.
        assert verify_stream_token(token, stream_name=session.stream_name,
                                   role=ROLE_SUBSCRIBER) == viewer.id


def test_the_playlists_own_tags_come_back_untouched(origin):
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    body = ask_playlist(session, tok(session, viewer.id)).content.decode()
    for tag in ('#EXTM3U', '#EXT-X-TARGETDURATION:2', '#EXT-X-MEDIA-SEQUENCE:12',
                '#EXTINF:2.0, no desc'):
        assert tag in body


def test_a_uri_attribute_inside_a_tag_is_signed_too(origin):
    # None appear with the current srs.conf — TS segments, hls_keys off —
    # but an fMP4 init segment or an encryption key is fetched exactly like
    # a segment, and an unsigned one would be a black screen with nothing
    # in any log to explain it.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    origin.body = '#EXT-X-MAP:URI="%s-init.mp4"\n%s-12.ts\n' % (
        session.stream_name, session.stream_name)

    body = ask_playlist(session, tok(session, viewer.id)).content.decode()
    assert '#EXT-X-MAP:URI="%s-init.mp4?token=' % session.stream_name in body
    assert body.count('token=') == 2


def test_the_segment_token_outlives_the_playlist_window_and_not_much_more(origin):
    # It has to cover the segments the playlist lists (hls_window 20) plus a
    # reload, or an honest viewer's oldest segment 403s mid-watch. Past that
    # it is how long a copied segment URL keeps working.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    body = ask_playlist(session, tok(session, viewer.id)).content.decode()
    token = parse_qs(segment_lines(body)[0].partition('?')[2])['token'][0]

    assert live_hooks.HLS_SEGMENT_TOKEN_TTL_SECONDS > 20
    alive = verify_stream_token(token, stream_name=session.stream_name,
                               role=ROLE_SUBSCRIBER)
    assert alive == viewer.id
    # Long past its own lifetime it is refused, and long before the hour the
    # playlist token gets.
    expired = verify_stream_token(
        token, stream_name=session.stream_name, role=ROLE_SUBSCRIBER,
        now=time.time() + live_hooks.HLS_SEGMENT_TOKEN_TTL_SECONDS + 5)
    assert expired is None


def test_a_banned_viewer_gets_no_playlist(origin):
    # The one that matters most: the ban used to cut WebRTC only, and the
    # app's own fallback then moved the banned viewer onto HLS.
    session, owner = make_session()
    banned = User.objects.create_user(username='banned', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    origin.serving(session.stream_name)
    assert ask_playlist(session, tok(session, banned.id)).status_code == 403
    assert origin.calls == []           # refused before SRS is even asked


def test_an_anonymous_token_gets_no_playlist(origin):
    session, _ = make_session()
    origin.serving(session.stream_name)
    assert ask_playlist(session, tok(session, 0)).status_code == 403


def test_a_token_for_another_stream_or_role_gets_no_playlist(origin):
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    elsewhere = tok(session, viewer.id, stream='live_other')
    publish = make_stream_token(session.stream_name, ROLE_PUBLISHER, 60, user_id=viewer.id)
    assert ask_playlist(session, elsewhere).status_code == 403
    assert ask_playlist(session, publish).status_code == 403
    assert ask_playlist(session, 'garbage').status_code == 403
    assert ask_playlist(session).status_code == 403


def test_the_playlist_stops_once_the_session_is_not_live(origin):
    # A stream name outlives its session and the publisher may still be up,
    # so this is what stops a finished broadcast from serving on.
    viewer = User.objects.create_user(username='viewer', password='x')
    for status in ('scheduled', 'ended', 'cancelled'):
        session, _ = make_session(username='seller_%s' % status, status=status)
        origin.serving(session.stream_name)
        assert ask_playlist(session, tok(session, viewer.id)).status_code == 403, status


def test_an_unknown_stream_is_refused_even_with_a_valid_signature(origin):
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving('live_nosession')
    token = make_stream_token('live_nosession', ROLE_SUBSCRIBER, 60, user_id=viewer.id)
    assert ask_playlist(session, uri='/live/live_nosession.m3u8?token=%s' % token
                        ).status_code == 403


def test_a_stream_srs_has_no_playlist_for_is_a_404_not_a_refusal(origin):
    # Nobody is publishing yet, or SRS has disposed of the stream. The
    # viewer is entitled to watch; there is just nothing there.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.status, origin.body = 404, 'not found'
    assert ask_playlist(session, tok(session, viewer.id)).status_code == 404


def test_an_unreachable_srs_is_a_502_not_a_refusal(origin):
    # A 403 here would read as a ban to anyone reading the logs, and to the
    # app it is the same dead screen either way — so say which it is.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.unreachable = True
    assert ask_playlist(session, tok(session, viewer.id)).status_code == 502


def test_the_origin_fetch_is_shared_between_viewers_of_one_stream(origin):
    # 50 playlist reloads a second at a hundred viewers; signing is per
    # viewer, reading from SRS is not.
    session, _ = make_session()
    one = User.objects.create_user(username='one', password='x')
    two = User.objects.create_user(username='two', password='x')
    origin.serving(session.stream_name)

    first = ask_playlist(session, tok(session, one.id)).content.decode()
    second = ask_playlist(session, tok(session, two.id)).content.decode()
    assert len(origin.calls) == 1
    # Shared fetch, separate signatures.
    assert segment_lines(first) != segment_lines(second)


# -- the segments ------------------------------------------------------------

def test_a_segment_from_the_signed_playlist_is_allowed_and_a_bare_one_is_not(origin):
    # End to end: what the playlist handed the player is what the proxy
    # accepts, and the same segment without it gets nothing.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    body = ask_playlist(session, tok(session, viewer.id)).content.decode()

    signed = segment_lines(body)[0]
    assert ask_segment('/live/%s' % signed).status_code == 204
    assert ask_segment('/live/%s-12.ts' % session.stream_name).status_code == 403
    assert ask_segment('/live/%s-12.ts?token=' % session.stream_name).status_code == 403
    assert ask_segment('/live/%s-12.ts?token=garbage' % session.stream_name).status_code == 403


def test_a_banned_viewers_segment_token_stops_working(origin):
    # They keep the tokens the last allowed playlist gave them until those
    # expire; what they do not get is a new playlist, and so no segment
    # newer than the ban.
    session, owner = make_session()
    banned = User.objects.create_user(username='banned', password='x')
    origin.serving(session.stream_name)
    body = ask_playlist(session, tok(session, banned.id)).content.decode()
    signed = segment_lines(body)[0]
    assert ask_segment('/live/%s' % signed).status_code == 204

    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    cache.clear()                       # the decision's few seconds running out
    assert ask_segment('/live/%s' % signed).status_code == 403
    assert ask_playlist(session, tok(session, banned.id)).status_code == 403


def test_neither_endpoint_covers_for_the_other(origin):
    # Caddy routes .m3u8 to one and .ts to the other; asking the wrong one
    # must not be a second way in.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    token = tok(session, viewer.id)
    assert ask_segment('/live/%s.m3u8?token=%s' % (session.stream_name, token)).status_code == 403
    assert ask_playlist(session, uri='/live/%s-12.ts?token=%s'
                        % (session.stream_name, token)).status_code == 403


def test_nothing_but_a_playlist_or_a_segment_is_authorized(origin):
    # Caddy answers 404 to these without asking, but the views refuse them
    # too: a DVR recording is an unbounded MP4 of a whole broadcast.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    token = tok(session, viewer.id)
    assert ask_segment('/live/dvr/%s.1700000000.mp4?token=%s'
                       % (session.stream_name, token)).status_code == 403
    assert ask_segment('/live/?token=%s' % token).status_code == 403
    assert ask_playlist(session, uri='/live/dvr/x.mp4?token=%s' % token).status_code == 403


def test_a_stream_name_cannot_walk_out_of_the_origin_path(origin):
    # The name is interpolated into the URL we fetch from SRS. A token can
    # only ever name a real stream, but this is the second lock.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    token = tok(session, viewer.id, stream='..%2f..%2fetc%2fpasswd')
    assert ask_playlist(session, uri='/live/..%%2f..%%2fetc%%2fpasswd.m3u8?token=%s'
                        % token).status_code == 403
    assert origin.calls == []


def test_both_endpoints_need_the_shared_secret(origin):
    # Without it they are an oracle for who is banned from which session,
    # answerable by anyone who finds the URL.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    token = tok(session, viewer.id)
    assert ask_playlist(session, token, secret='wrong').status_code == 403
    assert ask_playlist(session, token, secret='').status_code == 403
    assert ask_segment('/live/%s-12.ts?token=%s' % (session.stream_name, token),
                       secret='wrong').status_code == 403


def test_the_original_segment_request_must_be_a_read(origin):
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    body = ask_playlist(session, tok(session, viewer.id)).content.decode()
    signed = segment_lines(body)[0]
    assert ask_segment('/live/%s' % signed, method='POST').status_code == 403


# -- what it costs -----------------------------------------------------------

def test_the_policy_is_not_re_queried_for_every_segment(origin, django_assert_num_queries):
    # One request per two-second segment, so a ban query per request is not
    # an option: ~50/s at a hundred viewers on top of the playlist reloads.
    session, _ = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    origin.serving(session.stream_name)
    body = ask_playlist(session, tok(session, viewer.id)).content.decode()
    lines = segment_lines(body)
    with django_assert_num_queries(0):
        assert ask_segment('/live/%s' % lines[0]).status_code == 204
        assert ask_segment('/live/%s' % lines[1]).status_code == 204


def test_a_refusal_is_cached_as_well_as_an_allow(origin, django_assert_num_queries):
    # A refused player keeps retrying, which would otherwise make the
    # banned viewer the cheapest query generator in the system.
    session, owner = make_session()
    banned = User.objects.create_user(username='banned', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    origin.serving(session.stream_name)
    token = tok(session, banned.id)
    assert ask_playlist(session, token).status_code == 403
    with django_assert_num_queries(0):
        assert ask_playlist(session, token).status_code == 403


def test_the_cached_decision_is_per_viewer_not_per_stream(origin):
    session, owner = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    banned = User.objects.create_user(username='banned', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    origin.serving(session.stream_name)
    assert ask_playlist(session, tok(session, viewer.id)).status_code == 200
    assert ask_playlist(session, tok(session, banned.id)).status_code == 403
    assert ask_playlist(session, tok(session, viewer.id)).status_code == 200
