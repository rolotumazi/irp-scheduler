import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone as django_tz

from .models import (
    Interview,
    InterviewPanelist,
    Notification,
    PanelistAvailability,
    PreferenceRound,
    ProposalAssignment,
    RescheduleRequest,
    SchedulePublication,
    Timeslot,
    User,
)
from .notifications import enqueue_preference_request
from .solver import propose, publish


def make_slot(ref='W1D1-0930-01', room='Room 1', offset_hours=0, day_label='W1D1-Tue'):
    start = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc) + timedelta(hours=offset_hours)
    return Timeslot.objects.create(
        slot_ref=ref,
        day_label=day_label,
        start_at=start,
        end_at=start + timedelta(minutes=30),
        room_label=room,
        teams_link='https://teams.microsoft.com/l/meetup-join/xyz',
    )


def make_user(email, role, username=None):
    return User.objects.create_user(
        username=username or email.split('@')[0],
        email=email,
        role=role,
    )


class HealthCheckTests(TestCase):
    def test_healthz_returns_ok(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'ok')

    def test_loopback_always_in_allowed_hosts(self):
        # The container healthcheck hits 127.0.0.1; loopback must stay allowed
        # even when ALLOWED_HOSTS is locked to the public domain in prod,
        # otherwise the healthcheck gets a 400 and the container shows unhealthy.
        from django.conf import settings
        self.assertIn('127.0.0.1', settings.ALLOWED_HOSTS)
        self.assertIn('localhost', settings.ALLOWED_HOSTS)


class ProxySSLTests(TestCase):
    def test_forwarded_proto_https_marks_request_secure(self):
        # Caddy terminates TLS and forwards X-Forwarded-Proto; Django must trust
        # it so request.is_secure() is correct behind the proxy.
        request = RequestFactory().get('/', HTTP_X_FORWARDED_PROTO='https')
        self.assertTrue(request.is_secure())

    def test_plain_request_is_not_secure(self):
        request = RequestFactory().get('/')
        self.assertFalse(request.is_secure())


class TimeslotTests(TestCase):
    def test_slot_ref_is_unique(self):
        make_slot()
        with self.assertRaises(IntegrityError):
            make_slot(room='Room 2')

    def test_str(self):
        slot = make_slot()
        self.assertIn('W1D1-0930-01', str(slot))


class InterviewTests(TestCase):
    def setUp(self):
        self.slot = make_slot()
        self.student = make_user('amir@example.ac.uk', User.Role.INTERVIEWEE)

    def test_one_interview_per_timeslot(self):
        Interview.objects.create(
            external_id='IRP-001',
            title='Quantum foo',
            timeslot=self.slot,
            interviewee=self.student,
        )
        other_student = make_user('beatrice@example.ac.uk', User.Role.INTERVIEWEE)
        with self.assertRaises(IntegrityError):
            Interview.objects.create(
                external_id='IRP-002',
                title='Another',
                timeslot=self.slot,
                interviewee=other_student,
            )

    def test_one_interview_per_student(self):
        Interview.objects.create(
            external_id='IRP-001',
            title='Quantum foo',
            timeslot=self.slot,
            interviewee=self.student,
        )
        slot2 = make_slot(ref='W1D1-0930-02', room='Room 2')
        with self.assertRaises(IntegrityError):
            Interview.objects.create(
                external_id='IRP-002',
                title='Another',
                timeslot=slot2,
                interviewee=self.student,
            )

    def test_default_status_is_scheduled(self):
        interview = Interview.objects.create(
            external_id='IRP-001',
            title='Quantum foo',
            timeslot=self.slot,
            interviewee=self.student,
        )
        self.assertEqual(interview.status, Interview.Status.SCHEDULED)

    def test_interview_can_be_unscheduled(self):
        # Interviews exist before the solver places them, so timeslot is optional.
        interview = Interview.objects.create(
            external_id='IRP-100',
            title='Not yet placed',
            interviewee=self.student,
        )
        self.assertIsNone(interview.timeslot)

    def test_multiple_interviews_can_be_unscheduled(self):
        # NULLs are distinct, so the OneToOne doesn't block several unplaced ones.
        Interview.objects.create(
            external_id='IRP-101',
            title='A',
            interviewee=self.student,
        )
        Interview.objects.create(
            external_id='IRP-102',
            title='B',
            interviewee=make_user('cara@example.ac.uk', User.Role.INTERVIEWEE),
        )
        self.assertEqual(Interview.objects.filter(timeslot__isnull=True).count(), 2)


class InterviewPanelistTests(TestCase):
    def setUp(self):
        self.interview = Interview.objects.create(
            external_id='IRP-001',
            title='Quantum foo',
            timeslot=make_slot(),
            interviewee=make_user('amir@example.ac.uk', User.Role.INTERVIEWEE),
        )
        self.panelist = make_user('grace@example.ac.uk', User.Role.INTERVIEWER)

    def test_cannot_add_same_panelist_twice(self):
        InterviewPanelist.objects.create(interview=self.interview, user=self.panelist)
        with self.assertRaises(IntegrityError):
            InterviewPanelist.objects.create(interview=self.interview, user=self.panelist)


class RescheduleRequestTests(TestCase):
    def test_default_status_is_pending(self):
        interview = Interview.objects.create(
            external_id='IRP-001',
            title='Quantum foo',
            timeslot=make_slot(),
            interviewee=make_user('amir@example.ac.uk', User.Role.INTERVIEWEE),
        )
        req = RescheduleRequest.objects.create(
            interview=interview,
            requested_by=interview.interviewee,
            reason='clash',
        )
        self.assertEqual(req.status, RescheduleRequest.Status.PENDING)


def _write_xlsx(path, rows, headers=None, sheet='Meetings'):
    import openpyxl

    headers = headers or (
        'MeetingID', 'Date', 'DayLabel', 'StartTime', 'EndTime',
        'Subject', 'JoinUrl', 'MeetingObjectId', 'Status',
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers)
    for row in rows:
        ws.append(row)
    wb.save(path)


class ImportTimeslotsCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'bulk.xlsx'

    def _sample_rows(self):
        d = datetime(2026, 9, 1)
        return [
            ('W1D1-0930-01', d, 'W1D1-Tue', '09:30', '10:00', 'subj', 'https://teams/1', 'obj1', 'Created'),
            ('W1D1-0930-02', d, 'W1D1-Tue', '09:30', '10:00', 'subj', None, None, 'Pending'),
        ]

    def test_creates_and_updates_idempotently(self):
        _write_xlsx(self.path, self._sample_rows())
        call_command('import_timeslots', str(self.path))
        self.assertEqual(Timeslot.objects.count(), 2)

        created = Timeslot.objects.get(slot_ref='W1D1-0930-01')
        pending = Timeslot.objects.get(slot_ref='W1D1-0930-02')
        self.assertEqual(created.teams_link, 'https://teams/1')
        self.assertIsNone(pending.teams_link)
        self.assertEqual(created.room_label, 'Room 1')
        self.assertEqual(pending.room_label, 'Room 2')

        # Re-run: still 2 rows (update_or_create), teams_link picked up for the Pending row.
        updated_rows = [
            ('W1D1-0930-01', datetime(2026, 9, 1), 'W1D1-Tue', '09:30', '10:00', 'subj', 'https://teams/1', 'obj1', 'Created'),
            ('W1D1-0930-02', datetime(2026, 9, 1), 'W1D1-Tue', '09:30', '10:00', 'subj', 'https://teams/2', 'obj2', 'Created'),
        ]
        _write_xlsx(self.path, updated_rows)
        call_command('import_timeslots', str(self.path))
        self.assertEqual(Timeslot.objects.count(), 2)
        self.assertEqual(Timeslot.objects.get(slot_ref='W1D1-0930-02').teams_link, 'https://teams/2')

    def test_dry_run_writes_nothing(self):
        _write_xlsx(self.path, self._sample_rows())
        call_command('import_timeslots', str(self.path), '--dry-run')
        self.assertEqual(Timeslot.objects.count(), 0)

    def test_missing_column_aborts(self):
        _write_xlsx(
            self.path,
            [('W1D1-0930-01', datetime(2026, 9, 1), '09:30')],
            headers=('MeetingID', 'Date', 'StartTime'),
        )
        with self.assertRaises(CommandError):
            call_command('import_timeslots', str(self.path))

    def test_bad_meeting_id_aborts(self):
        _write_xlsx(self.path, [
            ('BROKEN', datetime(2026, 9, 1), 'W1D1-Tue', '09:30', '10:00', 'subj', 'url', 'obj', 'Created'),
        ])
        with self.assertRaises(CommandError):
            call_command('import_timeslots', str(self.path))
        self.assertEqual(Timeslot.objects.count(), 0)


INTERVIEW_HEADERS = ('ExternalID', 'Title', 'IntervieweeEmail', 'PanelistEmails')


class ImportInterviewsCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'interviews.xlsx'
        self.amir = make_user('amir@example.ac.uk', User.Role.INTERVIEWEE)
        self.bea = make_user('bea@example.ac.uk', User.Role.INTERVIEWEE)
        self.grace = make_user('grace@example.ac.uk', User.Role.INTERVIEWER)
        self.alan = make_user('alan@example.ac.uk', User.Role.INTERVIEWER)

    def _write(self, rows, headers=INTERVIEW_HEADERS):
        _write_xlsx(self.path, rows, headers=headers, sheet='Interviews')

    def _sample_rows(self):
        return [
            ('IRP-001', 'Quantum foo', 'amir@example.ac.uk', 'grace@example.ac.uk;alan@example.ac.uk'),
            ('IRP-002', 'Bar baz', 'bea@example.ac.uk', 'grace@example.ac.uk'),
        ]

    def test_creates_interviews_with_panels_unscheduled(self):
        self._write(self._sample_rows())
        call_command('import_interviews', str(self.path))

        self.assertEqual(Interview.objects.count(), 2)
        i1 = Interview.objects.get(external_id='IRP-001')
        self.assertIsNone(i1.timeslot)
        self.assertEqual(i1.interviewee, self.amir)
        self.assertEqual(i1.status, Interview.Status.SCHEDULED)
        self.assertEqual(set(i1.panelists.all()), {self.grace, self.alan})

    def test_idempotent_reimport_updates_title_and_panel(self):
        self._write(self._sample_rows())
        call_command('import_interviews', str(self.path))

        self._write([
            ('IRP-001', 'Quantum foo v2', 'amir@example.ac.uk', 'alan@example.ac.uk'),
            ('IRP-002', 'Bar baz', 'bea@example.ac.uk', 'grace@example.ac.uk'),
        ])
        call_command('import_interviews', str(self.path))

        self.assertEqual(Interview.objects.count(), 2)
        i1 = Interview.objects.get(external_id='IRP-001')
        self.assertEqual(i1.title, 'Quantum foo v2')
        self.assertEqual(set(i1.panelists.all()), {self.alan})

    def test_unknown_interviewee_aborts(self):
        self._write([('IRP-001', 'T', 'nobody@example.ac.uk', 'grace@example.ac.uk')])
        with self.assertRaises(CommandError):
            call_command('import_interviews', str(self.path))
        self.assertEqual(Interview.objects.count(), 0)

    def test_interviewee_must_have_interviewee_role(self):
        # grace is an interviewer, not an interviewee.
        self._write([('IRP-001', 'T', 'grace@example.ac.uk', 'alan@example.ac.uk')])
        with self.assertRaises(CommandError):
            call_command('import_interviews', str(self.path))
        self.assertEqual(Interview.objects.count(), 0)

    def test_unknown_panelist_aborts(self):
        self._write([('IRP-001', 'T', 'amir@example.ac.uk', 'ghost@example.ac.uk')])
        with self.assertRaises(CommandError):
            call_command('import_interviews', str(self.path))
        self.assertEqual(Interview.objects.count(), 0)

    def test_panelist_must_have_interviewer_role(self):
        # bea is an interviewee, not an interviewer.
        self._write([('IRP-001', 'T', 'amir@example.ac.uk', 'bea@example.ac.uk')])
        with self.assertRaises(CommandError):
            call_command('import_interviews', str(self.path))
        self.assertEqual(Interview.objects.count(), 0)

    def test_duplicate_interviewee_in_file_aborts(self):
        self._write([
            ('IRP-001', 'A', 'amir@example.ac.uk', 'grace@example.ac.uk'),
            ('IRP-002', 'B', 'amir@example.ac.uk', 'alan@example.ac.uk'),
        ])
        with self.assertRaises(CommandError):
            call_command('import_interviews', str(self.path))
        self.assertEqual(Interview.objects.count(), 0)

    def test_missing_column_aborts(self):
        self._write([('IRP-001', 'T')], headers=('ExternalID', 'Title'))
        with self.assertRaises(CommandError):
            call_command('import_interviews', str(self.path))

    def test_dry_run_writes_nothing(self):
        self._write(self._sample_rows())
        call_command('import_interviews', str(self.path), '--dry-run')
        self.assertEqual(Interview.objects.count(), 0)


class ScheduleViewTests(TestCase):
    def setUp(self):
        self.amir = make_user('amir@example.ac.uk', User.Role.INTERVIEWEE)
        self.bea = make_user('bea@example.ac.uk', User.Role.INTERVIEWEE)
        self.grace = make_user('grace@example.ac.uk', User.Role.INTERVIEWER)
        self.alan = make_user('alan@example.ac.uk', User.Role.INTERVIEWER)

        self.slot1 = make_slot('W1D1-0930-01', 'Room 1', offset_hours=0, day_label='W1D1-Tue')
        self.slot2 = make_slot('W1D2-0930-01', 'Room 1', offset_hours=24, day_label='W1D2-Wed')

        self.i1 = Interview.objects.create(
            external_id='IRP-001', title='Quantum foo', timeslot=self.slot1, interviewee=self.amir,
        )
        InterviewPanelist.objects.create(interview=self.i1, user=self.grace)
        InterviewPanelist.objects.create(interview=self.i1, user=self.alan)

        self.i2 = Interview.objects.create(
            external_id='IRP-002', title='Bar baz', timeslot=self.slot2, interviewee=self.bea,
        )
        InterviewPanelist.objects.create(interview=self.i2, user=self.grace)

    # --- home / my sessions ---
    def test_home_anonymous_shows_signin_not_sessions(self):
        response = self.client.get(reverse('interviews:home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Sign in')
        self.assertNotContains(response, 'Quantum foo')

    def test_home_interviewee_sees_only_their_session(self):
        self.client.force_login(self.amir)
        response = self.client.get(reverse('interviews:home'))
        self.assertContains(response, 'Quantum foo')
        self.assertNotContains(response, 'Bar baz')

    def test_home_panelist_sees_all_their_sessions(self):
        self.client.force_login(self.grace)
        response = self.client.get(reverse('interviews:home'))
        self.assertContains(response, 'Quantum foo')
        self.assertContains(response, 'Bar baz')

    # --- full schedule ---
    def test_schedule_requires_login(self):
        response = self.client.get(reverse('interviews:schedule_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.url)

    def test_schedule_lists_all_with_teams_link(self):
        self.client.force_login(self.amir)
        response = self.client.get(reverse('interviews:schedule_list'))
        self.assertContains(response, 'Quantum foo')
        self.assertContains(response, 'Bar baz')
        self.assertContains(response, 'teams.microsoft.com')

    def test_schedule_filter_by_day(self):
        self.client.force_login(self.amir)
        response = self.client.get(reverse('interviews:schedule_list'), {'day': 'W1D2-Wed'})
        self.assertContains(response, 'Bar baz')
        self.assertNotContains(response, 'Quantum foo')

    def test_schedule_filter_by_person(self):
        self.client.force_login(self.amir)
        response = self.client.get(reverse('interviews:schedule_list'), {'person': 'alan@example.ac.uk'})
        self.assertContains(response, 'Quantum foo')
        self.assertNotContains(response, 'Bar baz')

    # --- session detail ---
    def test_detail_requires_login(self):
        response = self.client.get(reverse('interviews:schedule_detail', args=[self.i1.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.url)

    def test_detail_shows_panel_and_teams_link(self):
        self.client.force_login(self.amir)
        response = self.client.get(reverse('interviews:schedule_detail', args=[self.i1.pk]))
        self.assertContains(response, 'Quantum foo')
        self.assertContains(response, 'grace@example.ac.uk')
        self.assertContains(response, 'alan@example.ac.uk')
        self.assertContains(response, 'teams.microsoft.com')

    def test_detail_404_for_missing(self):
        self.client.force_login(self.amir)
        response = self.client.get(reverse('interviews:schedule_detail', args=[999999]))
        self.assertEqual(response.status_code, 404)


def make_open_round(min_slots=2, **kwargs):
    now = django_tz.now()
    return PreferenceRound.objects.create(
        name=kwargs.pop('name', 'Autumn round'),
        opens_at=kwargs.pop('opens_at', now - timedelta(hours=1)),
        closes_at=kwargs.pop('closes_at', now + timedelta(hours=1)),
        status=kwargs.pop('status', PreferenceRound.Status.OPEN),
        min_available_slots=min_slots,
        **kwargs,
    )


class AvailabilityViewTests(TestCase):
    def setUp(self):
        self.grace = make_user('grace@example.ac.uk', User.Role.INTERVIEWER)
        self.amir = make_user('amir@example.ac.uk', User.Role.INTERVIEWEE)
        self.round = make_open_round(min_slots=2)
        self.s1 = make_slot('S-1', 'Room 1', offset_hours=0)
        self.s2 = make_slot('S-2', 'Room 2', offset_hours=0)
        self.s3 = make_slot('S-3', 'Room 1', offset_hours=1)

    def _url(self):
        return reverse('interviews:availability', args=[self.round.pk])

    def test_requires_login(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.url)

    def test_interviewee_forbidden(self):
        self.client.force_login(self.amir)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_panelist_sees_grid(self):
        self.client.force_login(self.grace)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Room 1')

    def test_save_below_minimum_rejected(self):
        self.client.force_login(self.grace)
        response = self.client.post(self._url(), {'timeslot': [self.s1.id]})
        self.assertContains(response, 'at least 2')
        self.assertEqual(PanelistAvailability.objects.filter(panelist=self.grace).count(), 0)

    def test_save_meets_minimum(self):
        self.client.force_login(self.grace)
        response = self.client.post(self._url(), {'timeslot': [self.s1.id, self.s2.id]}, follow=True)
        self.assertEqual(response.status_code, 200)
        saved = set(
            PanelistAvailability.objects
            .filter(round=self.round, panelist=self.grace)
            .values_list('timeslot_id', flat=True)
        )
        self.assertEqual(saved, {self.s1.id, self.s2.id})

    def test_save_replaces_previous_selection(self):
        PanelistAvailability.objects.create(round=self.round, panelist=self.grace, timeslot=self.s1)
        PanelistAvailability.objects.create(round=self.round, panelist=self.grace, timeslot=self.s2)
        self.client.force_login(self.grace)
        self.client.post(self._url(), {'timeslot': [self.s2.id, self.s3.id]}, follow=True)
        saved = set(
            PanelistAvailability.objects
            .filter(round=self.round, panelist=self.grace)
            .values_list('timeslot_id', flat=True)
        )
        self.assertEqual(saved, {self.s2.id, self.s3.id})

    def test_prefill_shows_existing_checked(self):
        PanelistAvailability.objects.create(round=self.round, panelist=self.grace, timeslot=self.s1)
        self.client.force_login(self.grace)
        response = self.client.get(self._url())
        self.assertContains(response, f'value="{self.s1.id}" checked')

    def test_closed_round_does_not_save(self):
        self.round.status = PreferenceRound.Status.CLOSED
        self.round.save()
        self.client.force_login(self.grace)
        self.client.post(self._url(), {'timeslot': [self.s1.id, self.s2.id]}, follow=True)
        self.assertEqual(PanelistAvailability.objects.filter(panelist=self.grace).count(), 0)


class RoundResponsesViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin', email='admin@example.ac.uk', role=User.Role.ADMIN,
            is_staff=True, is_superuser=True,
        )
        self.grace = make_user('grace@example.ac.uk', User.Role.INTERVIEWER)
        self.alan = make_user('alan@example.ac.uk', User.Role.INTERVIEWER)
        self.round = make_open_round(min_slots=1)
        self.s1 = make_slot('S-1', 'Room 1')
        PanelistAvailability.objects.create(round=self.round, panelist=self.grace, timeslot=self.s1)

    def _url(self):
        return reverse('interviews:round_responses', args=[self.round.pk])

    def test_requires_staff(self):
        self.client.force_login(self.grace)  # interviewer, not staff
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    def test_staff_sees_response_counts(self):
        self.client.force_login(self.admin)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'grace@example.ac.uk')
        self.assertContains(response, 'alan@example.ac.uk')  # listed even though not responded
        # 1 of 2 interviewers responded
        self.assertContains(response, '1')


class NotificationTests(TestCase):
    def setUp(self):
        self.grace = make_user('grace@example.ac.uk', User.Role.INTERVIEWER)
        self.alan = make_user('alan@example.ac.uk', User.Role.INTERVIEWER)
        self.amir = make_user('amir@example.ac.uk', User.Role.INTERVIEWEE)
        self.s1 = make_slot('S-1', 'Room 1')

    def test_enqueue_is_idempotent(self):
        round_obj = make_open_round()
        enqueue_preference_request(round_obj, self.grace)
        enqueue_preference_request(round_obj, self.grace)
        self.assertEqual(Notification.objects.filter(recipient=self.grace).count(), 1)

    def test_preference_request_has_magic_link_to_form(self):
        round_obj = make_open_round()
        note, _ = enqueue_preference_request(round_obj, self.grace)
        self.assertIn('sesame', note.body)
        self.assertIn(f'/availability/{round_obj.pk}/', note.body)

    def test_send_notifications_delivers_and_marks_sent(self):
        round_obj = make_open_round()
        enqueue_preference_request(round_obj, self.grace)
        mail.outbox.clear()
        call_command('send_notifications')
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['grace@example.ac.uk'])
        note = Notification.objects.get(recipient=self.grace)
        self.assertEqual(note.status, Notification.Status.SENT)
        self.assertIsNotNone(note.sent_at)

    def test_send_notifications_does_not_resend(self):
        round_obj = make_open_round()
        enqueue_preference_request(round_obj, self.grace)
        call_command('send_notifications')
        mail.outbox.clear()
        call_command('send_notifications')  # nothing queued now
        self.assertEqual(len(mail.outbox), 0)

    def test_enqueue_requests_command_covers_all_interviewers(self):
        round_obj = make_open_round()
        call_command('enqueue_preference_requests', round_obj.pk)
        recipients = set(Notification.objects.values_list('recipient__email', flat=True))
        self.assertEqual(recipients, {'grace@example.ac.uk', 'alan@example.ac.uk'})

    def test_reminders_target_only_non_responders(self):
        now = django_tz.now()
        round_obj = make_open_round(
            opens_at=now - timedelta(days=2), closes_at=now + timedelta(days=1),
        )
        PanelistAvailability.objects.create(round=round_obj, panelist=self.grace, timeslot=self.s1)
        call_command('enqueue_preference_reminders')
        reminded = set(
            Notification.objects
            .filter(kind=Notification.Kind.PREFERENCE_REMINDER)
            .values_list('recipient__email', flat=True)
        )
        self.assertEqual(reminded, {'alan@example.ac.uk'})

    def test_reminders_skip_recently_opened_rounds(self):
        make_open_round(opens_at=django_tz.now() - timedelta(hours=1))
        call_command('enqueue_preference_reminders')
        self.assertEqual(
            Notification.objects.filter(kind=Notification.Kind.PREFERENCE_REMINDER).count(), 0,
        )

    def test_run_worker_once_sends_queued(self):
        round_obj = make_open_round()
        enqueue_preference_request(round_obj, self.grace)
        mail.outbox.clear()
        call_command('run_worker', '--once')
        self.assertEqual(len(mail.outbox), 1)


class SolverTests(TestCase):
    """The CP-SAT solver: hard availability, no panelist double-booking, and
    'preferences > compactness' objective. See interviews/solver.py."""

    def setUp(self):
        self.round = make_open_round(min_slots=0)
        self.grace = make_user('grace@example.ac.uk', User.Role.INTERVIEWER)
        self.alan = make_user('alan@example.ac.uk', User.Role.INTERVIEWER)
        self.amir = make_user('amir@example.ac.uk', User.Role.INTERVIEWEE)
        self.bea = make_user('bea@example.ac.uk', User.Role.INTERVIEWEE)

    def _interview(self, ext_id, interviewee, panelists, title='An interview'):
        i = Interview.objects.create(external_id=ext_id, title=title, interviewee=interviewee)
        for p in panelists:
            InterviewPanelist.objects.create(interview=i, user=p)
        return i

    def _avail(self, panelist, timeslot, state=PanelistAvailability.State.AVAILABLE):
        PanelistAvailability.objects.create(
            round=self.round, panelist=panelist, timeslot=timeslot, state=state,
        )

    def test_places_single_interview_in_only_available_slot(self):
        slot = make_slot('S-1', 'Room 1')
        i = self._interview('IRP-1', self.amir, [self.grace])
        self._avail(self.grace, slot)

        pub = propose(self.round)

        self.assertEqual(pub.status, SchedulePublication.Status.PROPOSED)
        assignment = pub.assignments.get(interview=i)
        self.assertEqual(assignment.timeslot, slot)

    def test_unplaceable_when_panelist_has_no_availability(self):
        make_slot('S-1', 'Room 1')
        i = self._interview('IRP-1', self.amir, [self.grace])
        # grace has no availability row at all → strict policy treats as unavailable
        pub = propose(self.round)
        a = pub.assignments.get(interview=i)
        self.assertIsNone(a.timeslot)
        self.assertIn('available', a.reason.lower())

    def test_no_panelist_double_booking_in_overlapping_window(self):
        # Same time window, two rooms → grace can only do one of the two.
        s1 = make_slot('S-1', 'Room 1', offset_hours=0)
        s2 = make_slot('S-2', 'Room 2', offset_hours=0)
        self._interview('IRP-1', self.amir, [self.grace])
        self._interview('IRP-2', self.bea, [self.grace])
        for s in (s1, s2):
            self._avail(self.grace, s)

        pub = propose(self.round)

        placed = pub.assignments.exclude(timeslot=None).count()
        self.assertEqual(placed, 1, 'grace can only sit on one of the two same-window interviews')

    def test_no_two_interviews_in_the_same_timeslot(self):
        s1 = make_slot('S-1', 'Room 1')
        # Two independent panels but only one slot → only one can be placed.
        self._interview('IRP-1', self.amir, [self.grace])
        self._interview('IRP-2', self.bea, [self.alan])
        self._avail(self.grace, s1)
        self._avail(self.alan, s1)

        pub = propose(self.round)

        slot_ids = list(pub.assignments.exclude(timeslot=None).values_list('timeslot_id', flat=True))
        self.assertEqual(len(slot_ids), 1)

    def test_prefers_preferred_slot_over_merely_available(self):
        # Two non-overlapping slots both feasible; "preferred" should win.
        s_a = make_slot('S-A', 'Room 1', offset_hours=0)
        s_b = make_slot('S-B', 'Room 1', offset_hours=1)
        i = self._interview('IRP-1', self.amir, [self.grace])
        self._avail(self.grace, s_a, state=PanelistAvailability.State.AVAILABLE)
        self._avail(self.grace, s_b, state=PanelistAvailability.State.PREFERRED)

        pub = propose(self.round)

        self.assertEqual(pub.assignments.get(interview=i).timeslot, s_b)


class PublishTests(TestCase):
    def setUp(self):
        self.round = make_open_round(min_slots=0)
        self.grace = make_user('grace@example.ac.uk', User.Role.INTERVIEWER)
        self.amir = make_user('amir@example.ac.uk', User.Role.INTERVIEWEE)
        self.bea = make_user('bea@example.ac.uk', User.Role.INTERVIEWEE)
        self.slot1 = make_slot('S-1', 'Room 1', offset_hours=0)
        self.slot2 = make_slot('S-2', 'Room 1', offset_hours=1)
        self.i_amir = Interview.objects.create(
            external_id='IRP-A', title='Amir', interviewee=self.amir,
        )
        InterviewPanelist.objects.create(interview=self.i_amir, user=self.grace)

    def _make_proposed_publication(self, *, assign_to=None):
        pub = SchedulePublication.objects.create(
            round=self.round, solver_status='OPTIMAL', objective_value=0,
        )
        ProposalAssignment.objects.create(
            publication=pub, interview=self.i_amir, timeslot=assign_to,
        )
        return pub

    def test_publish_writes_timeslot_back_to_interview(self):
        pub = self._make_proposed_publication(assign_to=self.slot1)

        publish(pub)

        self.i_amir.refresh_from_db()
        self.assertEqual(self.i_amir.timeslot, self.slot1)
        pub.refresh_from_db()
        self.assertEqual(pub.status, SchedulePublication.Status.PUBLISHED)
        self.assertIsNotNone(pub.published_at)

    def test_publish_enqueues_schedule_change_for_each_participant(self):
        pub = self._make_proposed_publication(assign_to=self.slot1)

        publish(pub)

        recipients = set(
            Notification.objects
            .filter(kind=Notification.Kind.SCHEDULE_CHANGE)
            .values_list('recipient__email', flat=True)
        )
        self.assertEqual(recipients, {'amir@example.ac.uk', 'grace@example.ac.uk'})

    def test_publish_does_not_notify_when_slot_unchanged(self):
        # Interview already in slot1; proposal places it in slot1 again.
        self.i_amir.timeslot = self.slot1
        self.i_amir.save()
        pub = self._make_proposed_publication(assign_to=self.slot1)

        publish(pub)

        self.assertFalse(
            Notification.objects.filter(kind=Notification.Kind.SCHEDULE_CHANGE).exists()
        )

    def test_publish_idempotent_via_dedupe(self):
        # Two consecutive publications for the same change should NOT duplicate
        # notifications for the same (publication, interview, user). Republishing
        # the *same* publication is blocked (status check); but enqueueing the same
        # schedule_change twice from one publication is a no-op.
        pub = self._make_proposed_publication(assign_to=self.slot1)
        publish(pub)
        count_before = Notification.objects.filter(kind=Notification.Kind.SCHEDULE_CHANGE).count()

        # A second proposal that moves it back to no-slot shouldn't double-fire
        # for the first publication's IDs.
        pub2 = SchedulePublication.objects.create(round=self.round, solver_status='OPTIMAL')
        ProposalAssignment.objects.create(publication=pub2, interview=self.i_amir, timeslot=self.slot2)
        publish(pub2)
        self.assertEqual(
            Notification.objects.filter(kind=Notification.Kind.SCHEDULE_CHANGE).count(),
            count_before + 2,  # one new notification each for amir + grace, keyed by pub2.pk
        )

    def test_publish_rejects_already_published(self):
        pub = self._make_proposed_publication(assign_to=self.slot1)
        publish(pub)
        with self.assertRaises(ValueError):
            publish(pub)

    def test_publish_handles_swap_without_oneto_one_collision(self):
        # A: slot1 → slot2, B: slot2 → slot1 (a straight swap). The OneToOne on
        # Interview.timeslot would collide if we rewrote in place; publish() must
        # clear first then assign.
        i_b = Interview.objects.create(external_id='IRP-B', title='Bea', interviewee=self.bea)
        self.i_amir.timeslot = self.slot1
        self.i_amir.save()
        i_b.timeslot = self.slot2
        i_b.save()

        pub = SchedulePublication.objects.create(round=self.round, solver_status='OPTIMAL')
        ProposalAssignment.objects.create(publication=pub, interview=self.i_amir, timeslot=self.slot2)
        ProposalAssignment.objects.create(publication=pub, interview=i_b, timeslot=self.slot1)

        publish(pub)

        self.i_amir.refresh_from_db()
        i_b.refresh_from_db()
        self.assertEqual(self.i_amir.timeslot, self.slot2)
        self.assertEqual(i_b.timeslot, self.slot1)
