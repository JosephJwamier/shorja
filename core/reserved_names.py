"""Usernames nobody may register or rename themselves to.

The platform's wallet account is looked up by its username
(services.commission.platform_account), so whoever owned that name owned every
commission. The rest would let a person pass for the platform in a chat.
"""
import re
import secrets
import unicodedata

from django.conf import settings

_NAMES = {
    'platform', 'admin', 'administrator', 'root', 'system', 'support', 'staff',
    'moderator', 'shorjeh', 'shorja', 'alshorja', 'الشورجة', 'الدعم_الفني',
    'الدعم', 'الادارة', 'الإدارة', 'المنصة', 'المنصّة',
}


def _fold(name):
    """One spelling for what reads the same: hamzas, taa marbuta, tatweel,
    diacritics, separators, letter case, and look-alike forms."""
    name = unicodedata.normalize('NFKD', (name or '').casefold())
    name = ''.join(c for c in name if unicodedata.category(c) not in ('Mn', 'Cf', 'Me'))
    name = re.sub('[أإآٱ]', 'ا', name)
    for a, b in (('ة', 'ه'), ('ى', 'ي'), ('ی', 'ي'), ('ک', 'ك'), ('ـ', ''), ('ؤ', 'و'), ('ئ', 'ي'),
                 ('چ', 'ج')):
        name = name.replace(a, b)
    name = name.translate(str.maketrans('01@$', 'oias'))
    return re.sub(r'[\W_]+', ' ', name).strip()


# «الشورجة» is also Baghdad's market and a shop there may well say so in its
# name ("أقمشة الشورجة"), so the bare word is refused only when it is the whole
# name; what nobody but the platform is, wherever it appears.
_WHOLE = {_f for _f in (
    'الشورجه', 'شورجه', 'shorjeh', 'shorja', 'alshorja', 'al shorja', 'platform', 'admin', 'support',
    'الدعم', 'الاداره', 'المنصه', 'الاداره العامه', 'متجر الشورجه', 'الشورجه الرسمي',
)}
_ANYWHERE = (
    'الدعم الفني', 'دعم فني', 'خدمه العملا', 'خدمه الزباين', 'اداره الشورجه', 'منصه الشورجه',
    'تطبيق الشورجه', 'دعم الشورجه', 'فريق الشورجه', 'الشورجه الرسميه', 'حساب الشورجه',
    'اداره التطبيق', 'اداره المنصه', 'shorjeh support', 'shorjeh official', 'shorjeh admin',
    'shorjeh team', 'customer support', 'customer service', 'technical support',
)


def poses_as_platform(name):
    folded = _fold(name)
    squeezed = folded.replace(' ', '')
    return squeezed in {w.replace(' ', '') for w in _WHOLE} or any(
        phrase.replace(' ', '') in squeezed for phrase in _ANYWHERE)


def random_username(taken):
    """'user_k3v9x2mq': says nothing about whose it is."""
    while True:
        name = 'user_' + ''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(8))
        if not taken(name):
            return name


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
    return name in _NAMES or name == platform.casefold() or looks_like_contact(name) \
        or poses_as_platform(name)
