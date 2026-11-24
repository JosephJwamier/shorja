"""Whose account a typed sign-in identifier means: one answer for the sign-in
itself and for the limit on guessing it."""
from django.contrib.auth import get_user_model

from .phone_utils import to_e164


def account_for(identifier):
    """The account, or None. What is shaped like a contact is a contact first:
    a username was taken first once, so registering someone's number as a
    username put a stranger's account in front of theirs."""
    User = get_user_model()
    identifier = str(identifier or '').strip()
    if not identifier:
        return None
    match = None
    if '@' in identifier:
        match = User.objects.filter(email=identifier.lower()).first()
    else:
        # Any spelling of the number reaches the one stored form.
        canonical = None if any(c.isalpha() for c in identifier) else to_e164(identifier)
        if canonical:
            match = User.objects.filter(phone=canonical).first()
    return match or User.objects.filter(username=identifier).first()
