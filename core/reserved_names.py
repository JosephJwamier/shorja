"""Usernames nobody may register or rename themselves to.

The platform's wallet account is looked up by its username
(services.commission.platform_account), so whoever owned that name owned every
commission. The rest would let a person pass for the platform in a chat.
"""
import re

from django.conf import settings

_NAMES = {
    'platform', 'admin', 'administrator', 'root', 'system', 'support', 'staff',
    'moderator', 'shorjeh', 'shorja', 'alshorja', 'الشورجة', 'الدعم_الفني',
    'الدعم', 'الادارة', 'الإدارة', 'المنصة', 'المنصّة',
}


def looks_like_contact(username):
    """A phone number or an email. The sign-in box takes all three, so a
    username shaped like someone's number would stand in front of it."""
    name = (username or '').strip()
    # No letter in it, rather than to_e164(): that drops letters, so it takes
    # 'user07801234567' for a number.
    return '@' in name or bool(re.fullmatch(r'[\W\d_]*\d[\W\d_]*', name))


def is_reserved_username(username):
    # Compared as MySQL's collation would: 'Platform ' is 'platform' to it.
    name = (username or '').strip().casefold()
    platform = getattr(settings, 'PLATFORM_ACCOUNT_USERNAME', 'platform')
    return name in _NAMES or name == platform.casefold() or looks_like_contact(name)
