"""The one shape of a 500 from an API view that caught something unexpected.

Views used to answer `except Exception as e` with `'errors': str(e)`, so the
exception's own text went to whoever triggered it: a MySQL IntegrityError
names tables and columns, a storage error names the bucket and endpoint, a
KeyError names internal fields. The client needs none of that; the operator
needs all of it, with the traceback, in the log — which Sentry collects.
"""
import logging
import sys

from django.db import IntegrityError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger('core.api')


ALREADY_THERE = 'هذا مسجّل مسبقاً'
_MYSQL_DUPLICATE = 1062


def is_duplicate(exc):
    """The database refusing a second of something there is one of: a double
    tap, not a fault."""
    return isinstance(exc, IntegrityError) and bool(exc.args) and exc.args[0] == _MYSQL_DUPLICATE


def _already_there():
    return Response({'success': False, 'code': 'duplicate', 'message': ALREADY_THERE, 'error': ALREADY_THERE},
                    status=status.HTTP_400_BAD_REQUEST)


def handler(exc, context):
    """REST_FRAMEWORK['EXCEPTION_HANDLER']."""
    return _already_there() if is_duplicate(exc) else exception_handler(exc, context)


def server_error(message):
    """Call inside an `except` block: logs the active exception with its
    traceback, and answers with [message] only."""
    if is_duplicate(sys.exc_info()[1]):
        return _already_there()
    logger.exception(message)
    return Response({'success': False, 'message': message},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)
