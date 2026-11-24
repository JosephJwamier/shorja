"""Proof that the person holding a session is the account's owner.

A session can be stolen; what it must not buy is the account itself. The phone
is where a password reset goes, so changing it (or the email, or deleting the
account) asks for what a thief of the session does not have: the password, or a
WhatsApp code sent to the number the account has *now*. The code is also the
only way for someone who signed up with Google and has no password.
"""
from django.core.cache import cache
from rest_framework import status
from rest_framework.exceptions import APIException

MAX_FAILURES = 10
WINDOW_SECONDS = 3600


class ProofRequired(APIException):
    """400 whose `code` the app reads to ask for the password or the code.

    Not a ValidationError: inside a serializer those are folded into field
    lists, and the code would stop being a plain string."""
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message, code='proof_required'):
        super().__init__({'code': code, 'message': message})


def take_attempt(key, limit, window):
    """Counts an attempt *before* it is judged: counting after lets any number
    of parallel guesses in before the first one is counted. False when spent."""
    cache.add(key, 0, window)
    try:
        return cache.incr(key) <= limit
    except ValueError:  # expired between the two
        cache.set(key, 1, window)
        return True


def check_own_password(user, password):
    """True/False, or ProofRequired when this account's guesses are spent."""
    key = 'own-password:%s' % user.pk
    if not take_attempt(key, MAX_FAILURES, WINDOW_SECONDS):
        raise ProofRequired('محاولات كثيرة — حاول بعد ساعة', code='too_many_attempts')
    if not user.check_password(password):
        return False
    cache.delete(key)
    return True


def require_proof(request):
    """Raises ProofRequired unless the request carries its user's password
    (`current_password`) or a valid code for their current phone (`code`)."""
    user = request.user
    password = str(request.data.get('current_password') or '')
    code = str(request.data.get('code') or '')

    if password and user.has_usable_password():
        if check_own_password(user, password):
            return
        raise ProofRequired('كلمة المرور غير صحيحة', code='wrong_password')
    if code and user.phone:
        from .otp_views import _check_code
        ok, _ = _check_code(user.phone, code)
        if ok:
            return
        raise ProofRequired('الرمز غير صحيح أو انتهت صلاحيته', code='wrong_code')
    if not user.has_usable_password() and not user.phone:
        return  # nothing to prove it with, and nothing a reset could go to
    raise ProofRequired('أكّد هويتك بكلمة المرور أو برمز واتساب — حدّث التطبيق إن لم يظهر لك الطلب')
