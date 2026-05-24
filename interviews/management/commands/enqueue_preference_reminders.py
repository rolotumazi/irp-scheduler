from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from interviews.models import PreferenceRound
from interviews.notifications import enqueue_preference_reminder, non_responders


class Command(BaseCommand):
    help = (
        'Queue a daily reminder for interviewers who have not responded to any '
        'currently-open round (deduped per day).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--min-age-hours', type=int, default=24,
            help='Only remind for rounds opened at least this long ago (default 24).',
        )

    def handle(self, *args, min_age_hours, **options):
        now = timezone.now()
        today = timezone.localdate()
        cutoff = now - timedelta(hours=min_age_hours)

        created = 0
        for round_obj in PreferenceRound.objects.filter(status=PreferenceRound.Status.OPEN):
            if not round_obj.is_open() or round_obj.opens_at > cutoff:
                continue
            for user in non_responders(round_obj):
                _, was_created = enqueue_preference_reminder(round_obj, user, today)
                created += was_created

        self.stdout.write(self.style.SUCCESS(f'preference reminders: {created} queued'))
