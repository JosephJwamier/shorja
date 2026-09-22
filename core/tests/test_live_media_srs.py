"""The SRS live-media scaffolding — token math, credentials shape, and the
SRS→Django hooks. None of it is routed yet (Agora still serves the app), so
the hook view is exercised directly with a request factory.
"""
import json
from unittest.mock import patch

import pytest
from rest_framework.test import APIRequestFactory

from core.live_hooks import SrsHookView
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
    TURN_PORT=443,
    TURN_SECRET='turn-secret',
)


@pytest.fixture(autouse=True)
def srs_settings(settings):
    for k, v in SRS_SETTINGS.items():
        setattr(settings, k, v)


def make_session(username='seller'):
    user = User.objects.create_user(username=username, password='x')
    store = UserStore.objects.create(user=user, name='s')
    return LiveSession.objects.create(store=store, title='t', status='live'), user


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
    assert any(u.startswith('turns:') for u in servers[0]['urls'])


def test_turn_is_omitted_when_not_configured(settings):
    settings.TURN_SECRET = ''
    assert turn_credentials('u7', 600) == []


# ---------------------------------------------------------------------------
# Credentials the app would receive
# ---------------------------------------------------------------------------

def test_publish_and_play_credentials_shape():
    session, owner = make_session()
    provider = get_live_media_provider()
    pub = provider.publish_credentials(session, owner)
    play = provider.play_credentials(session, owner)
    stream = session.agora_channel_name
    assert pub['provider'] == 'srs' and pub['role'] == 'publisher'
    assert pub['whip_url'].startswith('https://live.example.com/rtc/v1/whip/?app=live&stream=%s&token=' % stream)
    assert play['whep_url'].startswith('https://live.example.com/rtc/v1/whep/?app=live&stream=%s&token=' % stream)
    assert play['hls_url'].startswith('https://live.example.com/live/%s.m3u8' % stream)
    assert play['ice_servers'][0]['urls'] == ['stun:live.example.com:443']
    assert play['ice_servers'][1]['username'].endswith(':u%d' % owner.id)
    # A play token must not be usable to publish.
    play_token = play['whep_url'].rsplit('token=', 1)[1]
    assert verify_stream_token(play_token, stream_name=stream, role=ROLE_PUBLISHER) is None


def test_provider_refuses_to_run_unconfigured(settings):
    settings.SRS_PUBLIC_BASE_URL = ''
    session, owner = make_session()
    with pytest.raises(MediaProviderError):
        get_live_media_provider().publish_credentials(session, owner)


def test_default_provider_is_still_agora(settings):
    settings.LIVE_MEDIA_PROVIDER = 'agora'
    # The registry only knows srs today; agora is served by the untouched
    # join_token view. Asking the registry for it must fail loudly, not
    # silently hand out SRS credentials.
    with pytest.raises(MediaProviderError):
        get_live_media_provider()


# ---------------------------------------------------------------------------
# Hooks (SRS → Django), called directly — not routed yet
# ---------------------------------------------------------------------------

def hook(event, stream, token='', secret='hook-secret'):
    factory = APIRequestFactory()
    request = factory.post(
        '/api/live/srs/%s/?secret=%s' % (event, secret),
        data=json.dumps({'action': event, 'app': 'live', 'stream': stream,
                         'param': '?token=%s' % token}),
        content_type='application/json',
    )
    return SrsHookView.as_view()(request, event=event)


def test_hook_without_the_shared_secret_is_refused():
    session, _ = make_session()
    response = hook('on_publish', session.agora_channel_name, secret='wrong')
    assert response.status_code == 403


def test_publish_needs_a_publish_token_for_that_stream():
    session, owner = make_session()
    stream = session.agora_channel_name
    good = make_stream_token(stream, ROLE_PUBLISHER, 60, user_id=owner.id)
    play = make_stream_token(stream, ROLE_SUBSCRIBER, 60, user_id=owner.id)
    other = make_stream_token('live_other', ROLE_PUBLISHER, 60, user_id=owner.id)
    assert hook('on_publish', stream, good).data == {'code': 0}
    assert hook('on_publish', stream, play).data == {'code': 1}
    assert hook('on_publish', stream, other).data == {'code': 1}
    assert hook('on_publish', stream, '').data == {'code': 1}


def test_publish_is_refused_once_the_session_ended():
    session, owner = make_session()
    session.status = 'ended'; session.save()
    token = make_stream_token(session.agora_channel_name, ROLE_PUBLISHER, 60, user_id=owner.id)
    assert hook('on_publish', session.agora_channel_name, token).data == {'code': 1}


def test_unknown_stream_and_unknown_event_are_denied():
    session, _ = make_session()
    assert hook('on_publish', 'live_nope').data == {'code': 1}
    assert hook('on_whatever', session.agora_channel_name).status_code == 404


def test_play_consults_the_policy_with_the_tokens_user():
    session, owner = make_session()
    viewer = User.objects.create_user(username='viewer', password='x')
    token = make_stream_token(session.agora_channel_name, ROLE_SUBSCRIBER, 60, user_id=viewer.id)
    with patch('core.live_hooks._may_play', return_value=True) as policy:
        assert hook('on_play', session.agora_channel_name, token).data == {'code': 0}
    policy.assert_called_once()
    assert policy.call_args.args[1] == viewer.id
    with patch('core.live_hooks._may_play', return_value=False):
        assert hook('on_play', session.agora_channel_name, token).data == {'code': 1}


def test_play_with_a_bad_token_never_reaches_the_policy():
    session, _ = make_session()
    with patch('core.live_hooks._may_play') as policy:
        assert hook('on_play', session.agora_channel_name, 'nope').data == {'code': 1}
    policy.assert_not_called()


def test_ban_model_is_available_to_the_policy():
    # Pins the shape _may_play can rely on: session.bans filtered by user_id.
    session, owner = make_session()
    banned = User.objects.create_user(username='banned', password='x')
    LiveSessionBan.objects.create(session=session, user=banned, banned_by=owner)
    assert session.bans.filter(user_id=banned.id).exists()
