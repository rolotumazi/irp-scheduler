from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.db.models import F, Min, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from sesame.utils import get_query_string

from .forms import LoginRequestForm
from .models import Interview, InterviewPanelist, Timeslot, User


def healthz(request):
    """Liveness probe for the compose healthcheck and post-deploy smoke test."""
    return HttpResponse('ok', content_type='text/plain')


def _ordered_by_start(qs):
    # Unscheduled interviews (timeslot is NULL) sort last, deterministically
    # across SQLite and Postgres.
    return qs.order_by(F('timeslot__start_at').asc(nulls_last=True))


def home(request):
    sessions = None
    if request.user.is_authenticated:
        # Membership via a subquery (not an M2M join) so we avoid duplicate rows
        # and the Postgres "SELECT DISTINCT + ORDER BY joined column" error.
        panel_ids = InterviewPanelist.objects.filter(user=request.user).values('interview_id')
        sessions = _ordered_by_start(
            Interview.objects
            .filter(Q(interviewee=request.user) | Q(pk__in=panel_ids))
            .select_related('timeslot', 'interviewee')
            .prefetch_related('panelists')
        )
    return render(request, 'interviews/home.html', {'sessions': sessions})


@login_required
def schedule_list(request):
    interviews = (
        Interview.objects
        .select_related('timeslot', 'interviewee')
        .prefetch_related('panelists')
    )

    day = request.GET.get('day', '').strip()
    person = request.GET.get('person', '').strip()

    if day:
        interviews = interviews.filter(timeslot__day_label=day)
    if person:
        panel_match = InterviewPanelist.objects.filter(
            Q(user__email__icontains=person) | Q(user__last_name__icontains=person)
        ).values('interview_id')
        interviews = interviews.filter(
            Q(interviewee__email__icontains=person)
            | Q(interviewee__last_name__icontains=person)
            | Q(pk__in=panel_match)
        )

    # Distinct day labels, ordered by when that day starts (GROUP BY day_label,
    # ORDER BY min(start_at)) — avoids the DISTINCT+ORDER BY pitfall.
    days = [
        row['day_label']
        for row in Timeslot.objects.values('day_label')
        .annotate(first_start=Min('start_at'))
        .order_by('first_start')
    ]

    return render(request, 'interviews/schedule_list.html', {
        'interviews': _ordered_by_start(interviews),
        'days': days,
        'day': day,
        'person': person,
    })


@login_required
def schedule_detail(request, pk):
    interview = get_object_or_404(
        Interview.objects.select_related('timeslot', 'interviewee').prefetch_related('panelists'),
        pk=pk,
    )
    return render(request, 'interviews/schedule_detail.html', {'interview': interview})


def login_request(request):
    if request.user.is_authenticated:
        return redirect('interviews:home')

    if request.method == 'POST':
        form = LoginRequestForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email'].lower().strip()
            user = User.objects.filter(email__iexact=email, is_active=True).first()
            if user is not None:
                _send_magic_link(user, request)
            # Always show the same confirmation — avoid leaking which emails exist.
            return render(request, 'interviews/login_sent.html', {'email': email})
    else:
        form = LoginRequestForm()

    return render(request, 'interviews/login_request.html', {'form': form})


def logout_view(request):
    logout(request)
    return redirect('interviews:home')


def _send_magic_link(user, request):
    query_string = get_query_string(user)
    path = reverse('interviews:login_consume')
    link = f'{settings.SITE_URL}{path}{query_string}'

    send_mail(
        subject='Your Schedule IRP sign-in link',
        message=(
            f'Hello {user.get_full_name() or user.email},\n\n'
            f'Click the link below to sign in. It expires in 15 minutes and can only be used once.\n\n'
            f'{link}\n\n'
            f'If you did not request this link, you can safely ignore this email.\n'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )
