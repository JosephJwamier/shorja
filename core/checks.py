"""
Deployment checks that need a database connection. Registered with
Tags.database, so they run on `manage.py migrate` and `manage.py check
--database default` — every deploy path — without touching the DB on
ordinary commands.
"""
from django.conf import settings
from django.core.checks import Error, Tags, register
from django.db import connections


@register(Tags.database)
def mysql_time_zone_tables_loaded(app_configs, databases=None, **kwargs):
    """With TIME_ZONE != 'UTC' and USE_TZ=True, Django's MySQL backend wraps
    every date truncation / __date lookup in CONVERT_TZ(col, 'UTC', <zone>).
    MySQL evaluates that from its own mysql.time_zone_* tables, which are
    EMPTY on a fresh install — CONVERT_TZ then returns NULL and every
    per-day analytics query, admin date filter and ledger period silently
    returns nothing (or raises "Database returned an invalid datetime
    value"). Refuse to deploy in that state rather than discover it from
    an empty sales chart."""
    errors = []
    for alias in databases or []:
        connection = connections[alias]
        if connection.vendor != 'mysql' or not settings.USE_TZ or settings.TIME_ZONE == 'UTC':
            continue
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT CONVERT_TZ('2000-01-01 00:00:00', 'UTC', %s)", [settings.TIME_ZONE]
            )
            converted = cursor.fetchone()[0]
        if converted is None:
            errors.append(Error(
                f"MySQL ({alias}) has no time zone definitions for '{settings.TIME_ZONE}': "
                "CONVERT_TZ returns NULL, so every date-based query would be wrong.",
                hint=(
                    "Load the time zone tables once on the DB server: "
                    "`mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root -p mysql` "
                    "(Linux). On Windows use MySQL's downloadable timezone SQL package."
                ),
                id='core.E001',
            ))
    return errors
