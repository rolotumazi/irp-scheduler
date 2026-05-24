import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from .models import Interview, InterviewPanelist, RescheduleRequest, Timeslot, User


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
