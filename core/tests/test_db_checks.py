"""
core/checks.py — the MySQL time-zone-tables deployment check.

The real database is exercised once (the tables are loaded in every
environment this suite runs in, or the whole suite would fail on date
lookups); the failure path is simulated by making CONVERT_TZ return NULL,
which is exactly what MySQL does when mysql.time_zone_* is empty.
"""
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from core import checks

pytestmark = pytest.mark.django_db


def test_passes_against_the_real_database():
    assert checks.mysql_time_zone_tables_loaded(None, databases=['default']) == []


@override_settings(TIME_ZONE='UTC')
def test_skipped_entirely_when_time_zone_is_utc():
    # No CONVERT_TZ is ever emitted for UTC, so nothing to verify.
    with patch('core.checks.connections') as connections:
        assert checks.mysql_time_zone_tables_loaded(None, databases=['default']) == []
        connections.__getitem__.return_value.cursor.assert_not_called()


def test_reports_error_when_convert_tz_returns_null():
    cursor = MagicMock()
    cursor.fetchone.return_value = (None,)
    connection = MagicMock(vendor='mysql')
    connection.cursor.return_value.__enter__.return_value = cursor
    with patch('core.checks.connections', {'default': connection}):
        errors = checks.mysql_time_zone_tables_loaded(None, databases=['default'])
    assert [e.id for e in errors] == ['core.E001']
    assert 'Asia/Baghdad' in errors[0].msg
    assert 'mysql_tzinfo_to_sql' in errors[0].hint


def test_non_mysql_backends_are_ignored():
    connection = MagicMock(vendor='postgresql')
    with patch('core.checks.connections', {'default': connection}):
        assert checks.mysql_time_zone_tables_loaded(None, databases=['default']) == []
    connection.cursor.assert_not_called()


def test_no_databases_requested_means_no_queries():
    with patch('core.checks.connections') as connections:
        assert checks.mysql_time_zone_tables_loaded(None, databases=None) == []
        connections.__getitem__.assert_not_called()
