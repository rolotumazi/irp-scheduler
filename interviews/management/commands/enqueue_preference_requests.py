from django.core.management.base import BaseCommand, CommandError

from interviews.models import PreferenceRound, User
from interviews.notifications import enqueue_preference_request


class Command(BaseCommand):
    help = 'Queue a preference-request email for every active interviewer in a round.'

    def add_arguments(self, parser):
        parser.add_argument('round_id', type=int)

    def handle(self, *args, round_id, **options):
        try:
            round_obj = PreferenceRound.objects.get(pk=round_id)
        except PreferenceRound.DoesNotExist:
            raise CommandError(f'no PreferenceRound with id {round_id}')

        created = 0
        for user in User.objects.filter(role=User.Role.INTERVIEWER, is_active=True):
            _, was_created = enqueue_preference_request(round_obj, user)
            created += was_created

        self.stdout.write(self.style.SUCCESS(
            f'preference requests for {round_obj.name}: {created} queued'
        ))
