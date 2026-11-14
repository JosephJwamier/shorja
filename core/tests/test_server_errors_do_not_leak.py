"""An unexpected exception's text never reaches the client.

Views answered `except Exception as e` with `'errors': str(e)`: a MySQL error
names tables and columns, a storage error the bucket and endpoint. They now
call core.api_errors.server_error, which logs the traceback (Sentry picks it
up) and answers with the view's message only.
"""
import ast
import logging
from pathlib import Path

from core.api_errors import server_error

VIEWS = Path(__file__).resolve().parents[1] / 'views.py'


def test_server_error_logs_the_exception_and_returns_only_the_message(caplog):
    with caplog.at_level(logging.ERROR, logger='core.api'):
        try:
            raise RuntimeError("(1452, 'Cannot add or update a child row: core_wallet')")
        except RuntimeError:
            response = server_error('Failed to create dispute')

    assert response.status_code == 500
    assert response.data == {'success': False, 'message': 'Failed to create dispute'}
    record = caplog.records[-1]
    assert record.exc_info and 'core_wallet' in str(record.exc_info[1])


def _inside_log_calls(body):
    """Every node within a logger.*(...) call: the log is where the text
    belongs, so those uses are fine."""
    inside = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in ('logger', 'logging', 'log')):
            inside.update(id(sub) for sub in ast.walk(node))
    return inside


def _text_of(node, name):
    """str(name), or name interpolated into an f-string."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'str' and node.args
            and isinstance(node.args[0], ast.Name) and node.args[0].id == name):
        return True
    return (isinstance(node, ast.FormattedValue)
            and isinstance(node.value, ast.Name) and node.value.id == name)


def test_no_catch_all_handler_in_views_sends_the_exception_text_back():
    """Guards the pattern itself, so a new view copying an old one fails here."""
    tree = ast.parse(VIEWS.read_text(encoding='utf-8'))
    offenders = []
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.name
                and isinstance(handler.type, ast.Name) and handler.type.id == 'Exception'):
            continue
        logged = _inside_log_calls(handler.body)
        for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
            if id(node) not in logged and _text_of(node, handler.name):
                offenders.append(node.lineno)
    assert offenders == [], f'exception text outside the log at lines {offenders}'
