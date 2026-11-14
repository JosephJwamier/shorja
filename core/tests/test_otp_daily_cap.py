"""A ceiling on WhatsApp codes sent per day, platform-wide.

otp_send throttles one address. An attacker with many addresses sending codes
to random numbers is stopped only by a cap on the total — and every message
is paid from the Wevlix balance. At 80% an error log reaches Sentry; past the
cap the endpoint answers like a passing outage (otp_unavailable), so it does
not tell the attacker when to come back.
"""
import logging
from unittest.mock import patch

import pytest
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from core.services.wevlix import WevlixError

pytestmark = pytest.mark.django_db


def _send(number, **patches):
    # A fresh client per number: these are different people, and the per-IP
    # throttle is not what is under test.
    with patch('core.otp_views.send_otp', return_value='msg', **patches) as sent:
        response = APIClient().post(reverse('phone-send-code'), {'phone': number},
                                    format='json')
    return response, sent


def _number(i):
    return f'077012345{i:02d}'


@override_settings(OTP_DAILY_SEND_CAP=3)
def test_sending_stops_at_the_daily_cap():
    for i in range(3):
        assert _send(_number(i))[0].status_code == 200

    response, sent = _send(_number(3))

    assert response.status_code == 503
    assert response.data['code'] == 'otp_unavailable'
    sent.assert_not_called()


@override_settings(OTP_DAILY_SEND_CAP=3)
def test_a_send_wevlix_refused_is_not_counted():
    assert _send(_number(0), side_effect=WevlixError('down'))[0].status_code == 502
    for i in range(1, 4):
        assert _send(_number(i))[0].status_code == 200


@override_settings(OTP_DAILY_SEND_CAP=3)
def test_asking_again_for_a_live_code_costs_nothing():
    assert _send(_number(0))[0].status_code == 200
    for _ in range(5):
        _send(_number(0))   # "already sent" — no message, no slot
    assert _send(_number(1))[0].status_code == 200


@override_settings(OTP_DAILY_SEND_CAP=10)
def test_eighty_percent_of_the_cap_raises_an_alert(caplog):
    with caplog.at_level(logging.ERROR, logger='core.otp_views'):
        for i in range(8):
            _send(_number(i))
    assert any('of today' in r.getMessage() for r in caplog.records
               if r.levelno == logging.ERROR)
