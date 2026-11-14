"""The one shape of a 500 from an API view that caught something unexpected.

Views used to answer `except Exception as e` with `'errors': str(e)`, so the
exception's own text went to whoever triggered it: a MySQL IntegrityError
names tables and columns, a storage error names the bucket and endpoint, a
KeyError names internal fields. The client needs none of that; the operator
needs all of it, with the traceback, in the log — which Sentry collects.
"""
import logging

from rest_framework import status
from rest_framework.response import Response

logger = logging.getLogger('core.api')


def server_error(message):
    """Call inside an `except` block: logs the active exception with its
    traceback, and answers with [message] only."""
    logger.exception(message)
    return Response({'success': False, 'message': message},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR)
