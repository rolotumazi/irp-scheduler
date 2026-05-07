import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase

from .models import Interview, InterviewPanelist, RescheduleRequest, Timeslot, User


def make_slot(ref='W1D1-0930-01', room='Room 1', offset_hours=0):
    start = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc) + timedelta(hours=offset_hours)
    return Timeslot.objects.create(
        slot_ref=ref,
        day_label='W1D1-Tue',
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
