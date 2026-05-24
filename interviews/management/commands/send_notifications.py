from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from interviews.models import Notification


class Command(BaseCommand):
    help = 'Send all queued notifications via the configured email backend.'

    def handle(self, *args, **options):
        queued = (
            Notification.objects
            .filter(status=Notification.Status.QUEUED)
            .select_related('recipient')
        )
        sent = failed = 0
        for note in queued.iterator():
            try:
                send_mail(
                    subject=note.subject,
                    message=note.body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[note.recipient.email],
                    fail_silently=False,
                )
            except Exception as e:  # transport error — record and move on
                note.status = Notification.Status.FAILED
                note.error = str(e)
                note.save(update_fields=['status', 'error'])
                failed += 1
            else:
                note.status = Notification.Status.SENT
                note.sent_at = timezone.now()
                note.error = ''
                note.save(update_fields=['status', 'sent_at', 'error'])
                sent += 1

        self.stdout.write(self.style.SUCCESS(f'notifications: {sent} sent, {failed} failed'))
