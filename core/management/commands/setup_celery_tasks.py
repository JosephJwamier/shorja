"""Writes settings.CELERY_BEAT_SCHEDULE into the database scheduler's tables.

The schedule has one home, settings.py; django_celery_beat's DatabaseScheduler
copies it in on every beat start. This command does the same on demand, for a
server whose beat has not restarted yet. It used to delete every periodic task
and recreate a list of its own, so running it again dropped whatever only
settings knew about until beat restarted. Now it only adds and updates, and is
safe to run any number of times.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, PeriodicTask


def _crontab(schedule):
    return CrontabSchedule.objects.get_or_create(
        minute=str(schedule._orig_minute), hour=str(schedule._orig_hour),
        day_of_week=str(schedule._orig_day_of_week), day_of_month=str(schedule._orig_day_of_month),
        month_of_year=str(schedule._orig_month_of_year), timezone=settings.CELERY_TIMEZONE,
    )[0]


class Command(BaseCommand):
    help = 'Write the beat schedule from settings into the database (safe to re-run)'

    def handle(self, *args, **kwargs):
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            _, created = PeriodicTask.objects.update_or_create(
                name=name,
                defaults={'task': entry['task'], 'crontab': _crontab(entry['schedule']),
                          'interval': None, 'enabled': True},
            )
            self.stdout.write('%s %s' % ('Created' if created else 'Updated', name))
        self.stdout.write(self.style.SUCCESS(
            '%d periodic tasks in place' % len(settings.CELERY_BEAT_SCHEDULE)))
