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
