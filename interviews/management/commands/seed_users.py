from django.core.management.base import BaseCommand

from interviews.models import User


ADMINS = [
    # (email, username, first_name, last_name, password)
    ('admin@example.com', 'admin', 'Site', 'Admin', 'adminpass'),
]

INTERVIEWERS = [
    # (email, username, first_name, last_name)
    ('grace.hopper@example.ac.uk', 'ghopper', 'Grace', 'Hopper'),
    ('alan.turing@example.ac.uk', 'aturing', 'Alan', 'Turing'),
    ('ada.lovelace@example.ac.uk', 'alovelace', 'Ada', 'Lovelace'),
    ('donald.knuth@example.ac.uk', 'dknuth', 'Donald', 'Knuth'),
    ('barbara.liskov@example.ac.uk', 'bliskov', 'Barbara', 'Liskov'),
    ('edsger.dijkstra@example.ac.uk', 'edijkstra', 'Edsger', 'Dijkstra'),
    ('tony.hoare@example.ac.uk', 'thoare', 'Tony', 'Hoare'),
    ('frances.allen@example.ac.uk', 'fallen', 'Frances', 'Allen'),
]

INTERVIEWEES = [
    ('student01@example.ac.uk', 'student01', 'Amir', 'Khan'),
    ('student02@example.ac.uk', 'student02', 'Beatrice', 'Okonkwo'),
    ('student03@example.ac.uk', 'student03', 'Caspar', 'Nilsen'),
    ('student04@example.ac.uk', 'student04', 'Diya', 'Patel'),
    ('student05@example.ac.uk', 'student05', 'Elif', 'Demir'),
    ('student06@example.ac.uk', 'student06', 'Finn', "O'Brien"),
    ('student07@example.ac.uk', 'student07', 'Gita', 'Chakraborty'),
    ('student08@example.ac.uk', 'student08', 'Hamid', 'Rezaei'),
    ('student09@example.ac.uk', 'student09', 'Ingrid', 'Johansen'),
    ('student10@example.ac.uk', 'student10', 'Jiro', 'Tanaka'),
    ('student11@example.ac.uk', 'student11', 'Kira', 'Volkov'),
    ('student12@example.ac.uk', 'student12', 'Lukas', 'Novak'),
    ('student13@example.ac.uk', 'student13', 'Maya', 'Rosenberg'),
    ('student14@example.ac.uk', 'student14', 'Noah', 'Adeyemi'),
    ('student15@example.ac.uk', 'student15', 'Olena', 'Kravchuk'),
]


class Command(BaseCommand):
    help = 'Seed a realistic set of test users (idempotent). Admins get passwords; everyone else gets unusable passwords (magic-link only).'

    def handle(self, *args, **options):
        created, skipped = 0, 0

        for email, username, first, last, password in ADMINS:
            user, was_created = User.objects.get_or_create(
                email=email,
                defaults={
                    'username': username,
                    'first_name': first,
                    'last_name': last,
                    'role': User.Role.ADMIN,
                    'is_staff': True,
                    'is_superuser': True,
                },
            )
            if was_created:
                user.set_password(password)
                user.save()
                created += 1
                self.stdout.write(self.style.SUCCESS(f'admin   created: {email} (password: {password})'))
            else:
                skipped += 1

        for role, people in (
            (User.Role.INTERVIEWER, INTERVIEWERS),
            (User.Role.INTERVIEWEE, INTERVIEWEES),
        ):
            for email, username, first, last in people:
                user, was_created = User.objects.get_or_create(
                    email=email,
                    defaults={
                        'username': username,
                        'first_name': first,
                        'last_name': last,
                        'role': role,
                    },
                )
                if was_created:
                    user.set_unusable_password()
                    user.save()
                    created += 1
                else:
                    skipped += 1

        self.stdout.write(self.style.SUCCESS(f'\nSeed complete: {created} created, {skipped} skipped.'))
