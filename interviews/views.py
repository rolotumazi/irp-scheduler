from itertools import groupby

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, F, Min, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .forms import LoginRequestForm
from .models import (
    Interview,
    InterviewPanelist,
    PanelistAvailability,
    PreferenceRound,
    Timeslot,
    User,
)
from .notifications import build_login_link


def healthz(request):
    """Liveness probe for the compose healthcheck and post-deploy smoke test."""
    return HttpResponse('ok', content_type='text/plain')


def _ordered_by_start(qs):
    # Unscheduled interviews (timeslot is NULL) sort last, deterministically
    # across SQLite and Postgres.
    return qs.order_by(F('timeslot__start_at').asc(nulls_last=True))


def home(request):
    sessions = None
    open_round = None
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
        if request.user.role == User.Role.INTERVIEWER:
            open_round = (
                PreferenceRound.objects
                .filter(status=PreferenceRound.Status.OPEN)
                .order_by('-opens_at')
                .first()
            )
    return render(request, 'interviews/home.html', {'sessions': sessions, 'open_round': open_round})


@login_required
def availability(request, round_id):
    if request.user.role != User.Role.INTERVIEWER:
        raise PermissionDenied('Only panelists set availability.')

    round_obj = get_object_or_404(PreferenceRound, pk=round_id)
    timeslots = list(Timeslot.objects.order_by('start_at', 'room_label'))
    valid_ids = {t.id for t in timeslots}
    existing = set(
        PanelistAvailability.objects
        .filter(round=round_obj, panelist=request.user)
        .values_list('timeslot_id', flat=True)
    )

    if request.method == 'POST':
        if not round_obj.is_open():
            messages.error(request, 'This availability round is not open.')
            return redirect('interviews:availability', round_id=round_obj.pk)

        selected = {int(v) for v in request.POST.getlist('timeslot') if v.isdigit()} & valid_ids
        if len(selected) < round_obj.min_available_slots:
            messages.error(
                request,
                f'Please mark at least {round_obj.min_available_slots} timeslots '
                f'(you marked {len(selected)}).',
            )
            existing = selected  # keep their selection on the re-render
        else:
            with transaction.atomic():
                PanelistAvailability.objects.filter(round=round_obj, panelist=request.user).delete()
                PanelistAvailability.objects.bulk_create([
                    PanelistAvailability(
                        round=round_obj, panelist=request.user, timeslot_id=tid,
                        state=PanelistAvailability.State.AVAILABLE,
                    )
                    for tid in selected
                ])
            messages.success(request, f'Saved — {len(selected)} timeslots marked available.')
            return redirect('interviews:availability', round_id=round_obj.pk)

    days = [
        (day, list(slots))
        for day, slots in groupby(timeslots, key=lambda t: t.day_label)
    ]
    return render(request, 'interviews/availability.html', {
        'round': round_obj,
        'days': days,
        'existing': existing,
        'editable': round_obj.is_open(),
    })


@staff_member_required
def round_responses(request, round_id):
    round_obj = get_object_or_404(PreferenceRound, pk=round_id)
    counts = dict(
        PanelistAvailability.objects.filter(round=round_obj)
        .values('panelist')
        .annotate(n=Count('timeslot'))
        .values_list('panelist', 'n')
    )
    rows = [
        {'user': u, 'count': counts.get(u.id, 0), 'responded': u.id in counts}
        for u in User.objects.filter(role=User.Role.INTERVIEWER).order_by('email')
    ]
    return render(request, 'interviews/round_responses.html', {
        'round': round_obj,
        'rows': rows,
        'responded': sum(1 for r in rows if r['responded']),
        'total': len(rows),
    })


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
    link = build_login_link(user)

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
