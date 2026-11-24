"""Usernames nobody may register or rename themselves to.

The platform's wallet account is looked up by its username
(services.commission.platform_account), so whoever owned that name owned every
commission. The rest would let a person pass for the platform in a chat.
"""
from django.conf import settings

_NAMES = {
    'platform', 'admin', 'administrator', 'root', 'system', 'support', 'staff',
    'moderator', 'shorjeh', 'shorja', 'alshorja', 'الشورجة', 'الدعم_الفني',
    'الدعم', 'الادارة', 'الإدارة', 'المنصة', 'المنصّة',
}


def is_reserved_username(username):
    # Compared as MySQL's collation would: 'Platform ' is 'platform' to it.
    name = (username or '').strip().casefold()
    platform = getattr(settings, 'PLATFORM_ACCOUNT_USERNAME', 'platform')
    return name in _NAMES or name == platform.casefold()
