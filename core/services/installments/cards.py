"""Card numbers: validated, used once, never kept.

A Qi Card number is a PAN. The platform is not PCI-certified and has no reason
to become so, which dictates three rules enforced here and nowhere else:

  **Never stored.** The number exists in one request body and one outbound
  call to the financier. What is written to the database is the last four
  digits and whatever token the financier hands back (see base.Eligibility).

  **Never logged.** Not in a log line, not in an exception message, not in a
  validation error echoed back to the app. `redact` is the only way a number
  is allowed near a string that might be written down, and the validators
  below raise messages that contain no part of the input.

  **Checked locally first.** A mistyped digit should cost a form error, not a
  round trip that the financier answers with "not eligible" — which reads to
  the buyer as "your salary is too low" when the truth is a typo. Luhn catches
  almost every single-digit slip and every adjacent transposition.
"""
import re

from django.conf import settings

# Lengths a PAN can have (ISO/IEC 7812). Qi Card issues 16-digit cards today;
# the range stays open so a new BIN with a different length is a settings
# change rather than a code change.
DEFAULT_LENGTHS = (16,)

_NON_DIGITS = re.compile(r"[\s\-]+")


class InvalidCardNumber(ValueError):
    """The input is not a card number. The message is safe to show the buyer
    and contains none of what they typed."""


def normalize(raw):
    """Strips the spaces and dashes people type between groups."""
    return _NON_DIGITS.sub("", str(raw or ""))


def luhn_ok(digits):
    """The checksum every PAN carries in its last digit."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = ord(char) - 48
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def validate(raw, *, lengths=None, require_luhn=None):
    """Returns the normalized number, or raises [InvalidCardNumber].

    `require_luhn` defaults to the provider's setting so a financier whose
    account numbers are not Luhn-valid can be accommodated without weakening
    the check for the ones that are.
    """
    number = normalize(raw)
    if not number.isdigit():
        raise InvalidCardNumber("رقم البطاقة يجب أن يكون أرقاماً فقط")

    allowed = tuple(lengths or DEFAULT_LENGTHS)
    if len(number) not in allowed:
        expected = " أو ".join(str(n) for n in allowed)
        raise InvalidCardNumber("رقم البطاقة يجب أن يكون %s رقماً" % expected)

    if require_luhn is None:
        require_luhn = True
    if require_luhn and not luhn_ok(number):
        raise InvalidCardNumber("رقم البطاقة غير صحيح — تأكّد من الأرقام")

    return number


def last4(number):
    return normalize(number)[-4:]


def redact(number):
    """What may appear in a log line or a support screen: ****1234."""
    tail = last4(number)
    return ("****%s" % tail) if tail else "****"


def card_lengths_from(config):
    """Reads CARD_LENGTHS out of a provider config, tolerating the string form
    an environment variable arrives as ("16,19")."""
    raw = config.get("CARD_LENGTHS") or DEFAULT_LENGTHS
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return tuple(int(p) for p in parts) or DEFAULT_LENGTHS
    return tuple(int(n) for n in raw)


def scrub_sentry_event(event, hint=None):
    """Sentry `before_send`: drop card numbers out of captured request bodies.

    Without this, a 500 anywhere downstream of the eligibility view ships the
    PAN to an error tracker — the one place nobody thinks to look for it. The
    keys are the field names the API accepts; anything else named like a card
    number is dropped too, since being wrong in that direction costs a
    debugging detail and being wrong in the other costs a card number.
    """
    try:
        data = (event.get("request") or {}).get("data")
        if isinstance(data, dict):
            for key in list(data):
                if "card_number" in key.lower() or "card" == key.lower():
                    data[key] = "[redacted]"
    except Exception:  # pragma: no cover - never break error reporting
        pass
    return event


def require_luhn_from(config):
    """Reads REQUIRE_LUHN, defaulting to on."""
    value = config.get("REQUIRE_LUHN")
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "")
    return bool(value)


def default_lengths():
    """The lengths used when no provider config is at hand (tests, admin)."""
    return tuple(getattr(settings, "INSTALLMENT_CARD_LENGTHS", DEFAULT_LENGTHS))
