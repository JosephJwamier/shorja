from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask, CrontabSchedule


class Command(BaseCommand):
    help = 'Setup Celery Beat periodic tasks'

    def handle(self, *args, **kwargs):
        self.stdout.write('Setting up periodic tasks...')

        # Clear existing tasks
        PeriodicTask.objects.all().delete()
        self.stdout.write('Cleared existing tasks')

        # Create crontab schedules
        every_5_min, _ = CrontabSchedule.objects.get_or_create(minute='*/5')
        every_15_min, _ = CrontabSchedule.objects.get_or_create(minute='*/15')
        every_6_hours, _ = CrontabSchedule.objects.get_or_create(minute='0', hour='*/6')
        every_4_hours, _ = CrontabSchedule.objects.get_or_create(minute='0', hour='*/4')
        daily_1am, _ = CrontabSchedule.objects.get_or_create(minute='0', hour='1')
        daily_2am, _ = CrontabSchedule.objects.get_or_create(minute='0', hour='2')
        sunday_3am, _ = CrontabSchedule.objects.get_or_create(
            minute='0',
            hour='3',
            day_of_week='0'
        )

        # Create periodic tasks
        tasks = [
            (every_15_min, 'process-expired-reservations', 'core.tasks.process_expired_reservations'),
            (every_15_min, 'check-cancellation-eligibility', 'core.tasks.check_cancellation_eligibility'),
            (every_6_hours, 'release-payments-after-week', 'core.tasks.release_payments_after_week'),
            (every_4_hours, 'send-pending-reservation-reminders', 'core.tasks.send_pending_reservation_reminders'),
            (every_5_min, 'activate-upcoming-auctions', 'core.tasks.activate_upcoming_auctions'),
            (every_5_min, 'process-ended-auctions', 'core.tasks.process_ended_auctions'),
            (daily_1am, 'auto-close-resolved-disputes', 'core.tasks.auto_close_resolved_disputes'),
            (daily_2am, 'calculate-trust-scores', 'core.tasks.calculate_user_trust_scores'),
            (daily_2am, 'recompute-store-levels', 'core.tasks.recompute_store_levels'),
            (sunday_3am, 'cleanup-old-messages', 'core.tasks.cleanup_old_messages'),
            (every_15_min, 'auto-end-stale-live-sessions', 'core.tasks.auto_end_stale_live_sessions'),
            (sunday_3am, 'purge-archived-items', 'core.tasks.purge_archived_items'),
            (sunday_3am, 'decay-category-affinities', 'core.tasks.decay_category_affinities'),
        ]

        for schedule, name, task in tasks:
            PeriodicTask.objects.create(
                crontab=schedule,
                name=name,
                task=task
            )
            self.stdout.write(f'Created task: {name}')

        self.stdout.write(self.style.SUCCESS(f'Successfully created {len(tasks)} periodic tasks'))