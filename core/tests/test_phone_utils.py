"""Canonical Iraqi mobile numbers (core/phone_utils.py).

Pure functions, no DB. These guard registration, login and password
recovery, so every shape a user might type is pinned here rather than
discovered in production.
"""
import pytest

from core.phone_utils import is_iraqi_mobile, to_e164


class TestAcceptedShapes:
    @pytest.mark.parametrize('typed', [
        '07701234567',        # local, the usual form
        '7701234567',         # local without the leading zero
        '+9647701234567',     # E.164, already canonical
        '9647701234567',      # E.164 without the plus
        '009647701234567',    # international prefix instead of '+'
        '0770 123 4567',      # spaced
        '0770-123-4567',      # dashed
        '(0770) 123 4567',    # bracketed
    ])
    def test_every_written_form_of_one_number_collapses_to_one_string(self, typed):
        assert to_e164(typed) == '+9647701234567'

    @pytest.mark.parametrize('prefix', ['75', '77', '78', '79'])
    def test_all_four_carriers_in_service(self, prefix):
        assert to_e164(f'0{prefix}01234567') == f'+964{prefix}01234567'


class TestRejectedShapes:
    @pytest.mark.parametrize('typed', [
        '',
        None,
        '0770123456',      # nine digits — one short
        '077012345678',    # eleven digits — one long
        '07401234567',     # 74 is not an assigned mobile prefix
        '07001234567',     # 70 likewise
        '017184512',       # Baghdad landline: no SMS can reach it
        'abcdefghij',
        '+1 555 123 4567',  # a real number, wrong country
    ])
    def test_rejected(self, typed):
        assert to_e164(typed) == ''


class TestUniquenessIsTheWholePoint:
    def test_two_spellings_of_one_phone_produce_one_key(self):
        # The reason the canonical form is stored rather than compared: a
        # UNIQUE index sees strings, so without this both of these would be
        # accepted as two separate accounts on the same phone.
        assert to_e164('07701234567') == to_e164('+964 770 123 4567')

    def test_two_different_phones_stay_different(self):
        assert to_e164('07701234567') != to_e164('07701234568')


class TestPredicateDirectly:
    @pytest.mark.parametrize('national,expected', [
        ('7701234567', True),
        ('7501234567', True),
        ('7401234567', False),
        ('770123456', False),
        ('', False),
    ])
    def test_is_iraqi_mobile(self, national, expected):
        assert is_iraqi_mobile(national) is expected
