"""Helpers for building and enqueueing outbound email notifications.

Enqueue functions create a Notification row (idempotent via dedupe_key); the
`send_notifications` command later delivers it through the configured email
backend. Subjects/bodies are rendered at enqueue time and stored on the row.
"""

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from sesame.utils import get_query_string

from .models import Notification, PanelistAvailability, User


def _slot_label(slot):
    if slot is None:
        return '(no slot yet)'
    return f'{slot.slot_ref} — {slot.day_label} {slot.start_at:%H:%M}, {slot.room_label}'


def build_login_link(user, next_path=None):
    """A single-use magic-link URL that signs `user` in, optionally landing on
    `next_path` afterwards."""
    link = f'{settings.SITE_URL}{reverse("interviews:login_consume")}{get_query_string(user)}'
    if next_path:
        link += f'&next={next_path}'
    return link


def enqueue(recipient, kind, subject, body, dedupe_key, **extra):
    """Create a queued Notification unless one with this dedupe_key exists."""
    return Notification.objects.get_or_create(
        dedupe_key=dedupe_key,
        defaults={
            'recipient': recipient,
            'kind': kind,
            'subject': subject,
            'body': body,
            **extra,
        },
    )


def non_responders(round_obj):
    """Interviewers with no availability rows in this round."""
    responded = (
        PanelistAvailability.objects.filter(round=round_obj)
        .values_list('panelist_id', flat=True)
    )
    return (
        User.objects.filter(role=User.Role.INTERVIEWER, is_active=True)
        .exclude(pk__in=responded)
    )


def enqueue_preference_request(round_obj, user):
    link = build_login_link(user, reverse('interviews:availability', args=[round_obj.pk]))
    ctx = {'user': user, 'round': round_obj, 'link': link}
    return enqueue(
        recipient=user,
        kind=Notification.Kind.PREFERENCE_REQUEST,
        subject=f'Please set your availability — {round_obj.name}',
        body=render_to_string('emails/preference_request.txt', ctx),
        dedupe_key=f'preference_request:{round_obj.pk}:{user.pk}',
        round=round_obj,
    )


def enqueue_schedule_change(interview, user, old_timeslot, new_timeslot, publication):
    """One notification per (publication, interview, user). Idempotent — safe to
    re-run a publish."""
    link = build_login_link(user, reverse('interviews:schedule_detail', args=[interview.pk]))
    ctx = {
        'user': user,
        'interview': interview,
        'old_slot': _slot_label(old_timeslot),
        'new_slot': _slot_label(new_timeslot),
        'link': link,
    }
    return enqueue(
        recipient=user,
        kind=Notification.Kind.SCHEDULE_CHANGE,
        subject=f'Your interview slot has changed — {interview.title}',
        body=render_to_string('emails/schedule_change.txt', ctx),
        dedupe_key=f'schedule_change:{publication.pk}:{interview.pk}:{user.pk}',
        interview=interview,
        publication=publication,
    )


def enqueue_preference_reminder(round_obj, user, day):
    link = build_login_link(user, reverse('interviews:availability', args=[round_obj.pk]))
    ctx = {'user': user, 'round': round_obj, 'link': link}
    return enqueue(
        recipient=user,
        kind=Notification.Kind.PREFERENCE_REMINDER,
        subject=f'Reminder: set your availability — {round_obj.name}',
        body=render_to_string('emails/preference_reminder.txt', ctx),
        # one reminder per panelist per day while the round is open
        dedupe_key=f'preference_reminder:{round_obj.pk}:{user.pk}:{day.isoformat()}',
        round=round_obj,
    )
