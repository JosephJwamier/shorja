"""Canonical form for Iraqi mobile numbers.

Until now User.phone held whatever the user typed and comparisons went
through normalize_phone_suffix() — the last nine digits, the part every
format shares. That was enough to *compare* two numbers, but it cannot make
one unique: '07701234567' and '+9647701234567' are the same phone and two
different strings, so a UNIQUE index on the column would happily hold both.

With the phone becoming the account's identity, the canonical form has to be
what gets stored. to_e164() produces it, and the database enforces it.
"""
import re

# Korek (75), Asiacell (77), Zain (78, 79).
VALID_MOBILE_PREFIXES = ('75', '77', '78', '79')


def to_e164(value):
    """'+964' + national number, or '' when the input is not an Iraqi mobile.

    Accepts every shape a user types: '07701234567', '7701234567',
    '+964 770 123 4567', '00964-770-123-4567'. Returning '' rather than
    raising lets callers decide whether a bad number is a validation error
    (registration) or simply not a match (lookup).
    """
    digits = re.sub(r'\D', '', value or '')
    if digits.startswith('00'):
        digits = digits[2:]

    if digits.startswith('964'):
        national = digits[3:]
    elif digits.startswith('0'):
        national = digits[1:]
    else:
        national = digits

    return '+964' + national if is_iraqi_mobile(national) else ''


def is_iraqi_mobile(national):
    """True when `national` is a valid Iraqi mobile national number.

    `national` is digits only, with no country code and no leading zero —
    i.e. what's left of '07701234567' after stripping the 0: '7701234567'.

    Mobiles only, and only the four prefixes in service: 75 (Korek), 77
    (Asiacell), 78 and 79 (Zain). Landlines are rejected on purpose — the
    number is the account's identity precisely because an SMS can reach it,
    and a landline cannot receive one.

    A prefix allow-list ages: if a carrier is assigned a new range, numbers
    on it are refused until this tuple is updated. That is the intended
    trade — a typo caught at signup costs the user one correction, while a
    wrong number saved is an account its owner can never recover.
    """
    return (
        len(national) == 10
        and national.startswith(VALID_MOBILE_PREFIXES)
    )
