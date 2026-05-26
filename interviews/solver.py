"""Scheduling engine: CP-SAT model that assigns interviews to timeslots.

Decision vars: x[i,t] ∈ {0,1}. Hard constraints:
 - each interview placed at most once (≤ 1, so partial proposals are allowed)
 - one interview per timeslot
 - x[i,t] = 0 if any panelist of i lacks an `available`/`preferred` row for t
 - a panelist may not appear in two interviews whose timeslots fall in the
   same wall-clock window (timeslots grouped by (start_at, end_at) — this app's
   imported grid has parallel rooms in identical windows, no partial overlaps)

Objective (preferences 10 : compactness 1, with placement dominating both):
    100000 * Σ x[i,t]                       # place as many as possible
  + 10     * Σ x[i,t] * pref_count(i,t)     # prefer slots panelists marked "preferred"
  - 1      * Σ y[p,d]                       # minimise # of days each panelist works

`propose()` writes a SchedulePublication + ProposalAssignment rows and returns
the publication. Unplaced interviews get an assignment with timeslot=NULL and a
short reason ("no slot has all panelists available." vs "no feasible placement
left after conflicts.").
"""

from collections import defaultdict

from django.db import transaction
from django.utils import timezone
from ortools.sat.python import cp_model

from .models import (
    Interview,
    PanelistAvailability,
    ProposalAssignment,
    SchedulePublication,
    Timeslot,
)
from .notifications import enqueue_schedule_change

PLACEMENT_WEIGHT = 100_000
PREFERENCE_WEIGHT = 10
COMPACTNESS_WEIGHT = 1
DEFAULT_TIME_LIMIT_SECONDS = 30.0


def _allowed_states():
    return {
        PanelistAvailability.State.AVAILABLE,
        PanelistAvailability.State.PREFERRED,
    }


@transaction.atomic
def propose(round_obj, *, created_by=None, time_limit_seconds=DEFAULT_TIME_LIMIT_SECONDS):
    """Solve and persist a proposed schedule for `round_obj`. Returns the
    SchedulePublication (status=proposed)."""
    interviews = list(
        Interview.objects.filter(status=Interview.Status.SCHEDULED)
        .prefetch_related('panelists')
        .order_by('id')
    )
    timeslots = list(Timeslot.objects.all().order_by('id'))

    avail_rows = PanelistAvailability.objects.filter(round=round_obj).values_list(
        'panelist_id', 'timeslot_id', 'state',
    )
    allowed = _allowed_states()
    avail = {(p_id, t_id): state for p_id, t_id, state in avail_rows}

    def panelist_ok(p_id, t_id):
        return avail.get((p_id, t_id)) in allowed

    panel_ids_by_interview = {
        i.id: list(i.panelists.values_list('id', flat=True)) for i in interviews
    }

    # Eligibility + preference score per (interview, timeslot).
    eligible = defaultdict(list)
    pref_score = {}
    for i in interviews:
        panel_ids = panel_ids_by_interview[i.id]
        if not panel_ids:
            continue
        for t in timeslots:
            if all(panelist_ok(p_id, t.id) for p_id in panel_ids):
                eligible[i.id].append(t)
                pref_score[(i.id, t.id)] = sum(
                    1 for p_id in panel_ids
                    if avail.get((p_id, t.id)) == PanelistAvailability.State.PREFERRED
                )

    model = cp_model.CpModel()
    x = {}
    for i in interviews:
        for t in eligible[i.id]:
            x[(i.id, t.id)] = model.NewBoolVar(f'x_{i.id}_{t.id}')

    # Each interview placed at most once.
    for i in interviews:
        terms = [x[(i.id, t.id)] for t in eligible[i.id]]
        if terms:
            model.Add(sum(terms) <= 1)

    # At most one interview per timeslot.
    by_timeslot = defaultdict(list)
    for (i_id, t_id), var in x.items():
        by_timeslot[t_id].append(var)
    for terms in by_timeslot.values():
        if len(terms) >= 2:
            model.Add(sum(terms) <= 1)

    # Panelist double-booking: per panelist, per (start_at, end_at) window.
    panelist_to_interviews = defaultdict(list)
    for i_id, p_ids in panel_ids_by_interview.items():
        for p_id in p_ids:
            panelist_to_interviews[p_id].append(i_id)

    windows = defaultdict(list)
    for t in timeslots:
        windows[(t.start_at, t.end_at)].append(t)

    for p_id, int_ids in panelist_to_interviews.items():
        for win_slots in windows.values():
            terms = []
            for i_id in int_ids:
                for t in win_slots:
                    if (i_id, t.id) in x:
                        terms.append(x[(i_id, t.id)])
            if len(terms) >= 2:
                model.Add(sum(terms) <= 1)

    # Day-spread penalty: y[p,d] = 1 iff panelist p has any session on day d.
    days = sorted({t.day_label for t in timeslots})
    slots_by_day = {d: [t for t in timeslots if t.day_label == d] for d in days}
    y = {}
    for p_id, int_ids in panelist_to_interviews.items():
        for d in days:
            y_pd = model.NewBoolVar(f'y_{p_id}_{d}')
            y[(p_id, d)] = y_pd
            for i_id in int_ids:
                for t in slots_by_day[d]:
                    if (i_id, t.id) in x:
                        model.Add(y_pd >= x[(i_id, t.id)])

    # Objective: placement dominates; preferences:compactness 10:1.
    placement_terms = list(x.values())
    pref_terms = [pref_score[k] * x[k] for k in x if pref_score.get(k)]
    compact_terms = list(y.values())
    if placement_terms or pref_terms or compact_terms:
        objective = 0
        if placement_terms:
            objective += PLACEMENT_WEIGHT * sum(placement_terms)
        if pref_terms:
            objective += PREFERENCE_WEIGHT * sum(pref_terms)
        if compact_terms:
            objective -= COMPACTNESS_WEIGHT * sum(compact_terms)
        model.Maximize(objective)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    status = solver.Solve(model)
    has_solution = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    placement = {}
    if has_solution:
        for (i_id, t_id), var in x.items():
            if solver.Value(var) == 1:
                placement[i_id] = t_id

    pub = SchedulePublication.objects.create(
        round=round_obj,
        created_by=created_by,
        solver_status=solver.StatusName(status),
        objective_value=int(solver.ObjectiveValue()) if has_solution else None,
    )

    for i in interviews:
        t_id = placement.get(i.id)
        if t_id is not None:
            ProposalAssignment.objects.create(
                publication=pub, interview=i, timeslot_id=t_id,
            )
        else:
            reason = (
                'No timeslot has all panelists available.'
                if not eligible[i.id]
                else 'No feasible placement left after resolving conflicts.'
            )
            ProposalAssignment.objects.create(
                publication=pub, interview=i, timeslot=None, reason=reason,
            )

    return pub


@transaction.atomic
def publish(publication):
    """Apply a proposal: write timeslots onto Interviews, diff vs prior live
    state, and enqueue schedule_change notifications for affected participants.
    Idempotent via Notification.dedupe_key. Returns the number of changed
    interviews."""
    if publication.status != SchedulePublication.Status.PROPOSED:
        raise ValueError(
            f'Only proposed publications can be published (status={publication.status}).'
        )

    assignments = list(
        publication.assignments.select_related('interview', 'timeslot')
    )

    diff = []  # (interview, old_timeslot, new_timeslot)
    for a in assignments:
        if a.timeslot_id is None:
            continue
        old = a.interview.timeslot
        new = a.timeslot
        if old != new:
            diff.append((a.interview, old, new))

    # Clear first, then reassign — Interview.timeslot is OneToOne, so a swap
    # like A:S1→S2 / B:S2→S3 would collide if we just rewrote in place.
    for interview, _, _ in diff:
        interview.timeslot = None
        interview.save(update_fields=['timeslot'])
    for interview, _, new in diff:
        interview.timeslot = new
        interview.save(update_fields=['timeslot'])

    publication.status = SchedulePublication.Status.PUBLISHED
    publication.published_at = timezone.now()
    publication.save(update_fields=['status', 'published_at'])

    for interview, old, new in diff:
        recipients = {interview.interviewee, *interview.panelists.all()}
        for user in recipients:
            if user.is_active and user.email:
                enqueue_schedule_change(interview, user, old, new, publication)

    return len(diff)
