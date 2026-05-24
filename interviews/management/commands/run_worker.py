import time

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Background worker loop: enqueue due preference reminders and send all '
        'queued notifications, repeating on an interval.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Run one cycle and exit.')
        parser.add_argument('--interval', type=int, default=60, help='Seconds between cycles.')

    def handle(self, *args, once, interval, **options):
        while True:
            call_command('enqueue_preference_reminders')
            call_command('send_notifications')
            if once:
                break
            time.sleep(interval)
