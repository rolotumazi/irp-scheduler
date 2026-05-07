from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        INTERVIEWER = 'interviewer', 'Interviewer'
        INTERVIEWEE = 'interviewee', 'Interviewee'

    email = models.EmailField(unique=True)
    role = models.CharField(max_length=16, choices=Role.choices)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return f'{self.get_full_name() or self.username} <{self.email}>'


class Timeslot(models.Model):
    slot_ref = models.CharField(max_length=32, unique=True)
    day_label = models.CharField(max_length=32)
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()
    room_label = models.CharField(max_length=32)
    teams_link = models.URLField(max_length=1024, blank=True, null=True)

    class Meta:
        ordering = ['start_at', 'room_label']

    def __str__(self):
        return f'{self.slot_ref} ({self.day_label} {self.room_label})'


class Interview(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = 'scheduled', 'Scheduled'
        CANCELLED = 'cancelled', 'Cancelled'
        COMPLETED = 'completed', 'Completed'

    external_id = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=255)
    timeslot = models.OneToOneField(
        Timeslot,
        on_delete=models.PROTECT,
        related_name='interview',
    )
    interviewee = models.OneToOneField(
        User,
        on_delete=models.PROTECT,
        related_name='interview_as_interviewee',
        limit_choices_to={'role': User.Role.INTERVIEWEE},
    )
    panelists = models.ManyToManyField(
        User,
        through='InterviewPanelist',
        related_name='interviews_as_panelist',
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.SCHEDULED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.external_id}: {self.title}'


class InterviewPanelist(models.Model):
    interview = models.ForeignKey(Interview, on_delete=models.CASCADE)
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        limit_choices_to={'role': User.Role.INTERVIEWER},
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['interview', 'user'],
                name='unique_panelist_per_interview',
            ),
        ]
        indexes = [models.Index(fields=['user'])]

    def __str__(self):
        return f'{self.user.email} on {self.interview.external_id}'


class RescheduleRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        FORWARDED = 'forwarded', 'Forwarded'
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'
        WITHDRAWN = 'withdrawn', 'Withdrawn'

    interview = models.ForeignKey(
        Interview,
        on_delete=models.CASCADE,
        related_name='reschedule_requests',
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='reschedule_requests',
    )
    reason = models.TextField(blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    external_ref = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'reschedule #{self.pk} for {self.interview.external_id} ({self.status})'
