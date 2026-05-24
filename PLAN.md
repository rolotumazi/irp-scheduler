# Schedule IRP — Platform Plan (v2)

A web application that **collects panelist availability, computes the interview
schedule, and keeps everyone informed by email**. ~300 student project
presentations ("interviews") are placed across 8 working days into a fixed pool
of pre-created Microsoft Teams meetings. Each participant signs in to see their
sessions and the correct Teams link for their current assignment.

> **What changed from v1.** The original plan scoped this as a *thin, read-mostly
> surface* over an external scheduler, with email and availability collection as
> explicit non-goals. That has been reversed by decision:
> - **This app now owns the scheduling logic** (was: external service).
> - **This app collects panelist availability** (was: handled upstream).
> - **Email notifications are in scope** (was: deferred to post-v1).
> - **Teams integration is unchanged**: keep importing the pre-created meeting
>   pool from xlsx and surface the right link per assignment. No Microsoft Graph
>   API.
>
> The inbound "external scheduler" API and the outbound reschedule webhook from
> v1 are therefore **removed**; their responsibilities move inside this app.

---

## 1. Goals & Non-Goals

### Goals (v2)
- Single dashboard where each participant sees their sessions and Teams links.
- **Collect availability/preferences from panelists** via a self-serve form,
  opened and reminded by email.
- **Compute an assignment** of interviews → timeslots that respects panelist
  availability and avoids double-booking, with an admin review-and-publish step.
- **Notify participants by email** on: schedule changes, preference requests
  (+ reminders to non-responders), session reminders, and reschedule outcomes.
- Let participants file a reschedule request; admin re-solves or adjusts and the
  app notifies the requester of the outcome.
- Import the pre-created Teams meeting pool from xlsx (the timeslot inventory).

### Non-goals (v2)
- Collecting **interviewee** availability (students are slotted around panels).
- Live Microsoft Graph API meeting creation/attendee management (links are
  pre-created and imported).
- `.ics` download / Google / Outlook calendar sync.
- In-app chat, comments, or feedback on presentations.
- Multi-tenancy / multiple concurrent events.

---

## 2. Stack

**Docker-first.** The app builds into a single image and runs as a
**self-contained `docker compose` stack** — `docker compose up` brings the whole
web-app online. We develop and run in containers from the start (dev/prod
parity), not just at deploy time. Unchanged from v1 except additions marked
**(new)**.

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.13 | Project requirement (`requires-python >=3.13`) |
| Framework | Django 6.0 | Free admin, built-in auth, **built-in Tasks framework (new)** |
| Database | **Postgres in a container** (named volume) | Self-contained, prod-appropriate |
| Auth | `django-sesame` magic-link | No passwords for a short event |
| Templates | Django templates + `htmx` | The availability grid is the only rich UI |
| Styling | Tailwind (or plain CSS) | Fast to iterate |
| **Solver (new)** | OR-Tools CP-SAT | Correct, handles hard + soft constraints at this size |
| **Background work (new)** | Django 6 Tasks framework (DB backend) + a worker container; cron container for time-based sends | Avoids Celery/Redis; matches modest scale |
| Audit | `django-simple-history` on `Interview` | Track edits + reconstruct schedule changes |
| **Email (locked)** | **Resend** (SMTP backend, verified domain + SPF/DKIM); console backend only for local dev | Single transport everywhere; clean DX |
| **Web server (new)** | Gunicorn (in the web container) | Standard Django prod server |
| **Reverse proxy / TLS (new)** | Caddy container, automatic HTTPS | One-command TLS; no manual cert wrangling |
| **Orchestration (new)** | `docker compose` (web + worker + cron + Postgres + Caddy) | Full self-contained stack |
| **CI/CD (new)** | GitHub Actions → **GHCR** → SSH deploy to dev VM | Build → test → deploy on push to main (§11) |
| Static files | WhiteNoise (in the web container) | No separate static host needed |
| Timezone | `Europe/London` everywhere | Only supported zone |

### Container topology

```
docker compose:
  caddy     → terminates TLS, reverse-proxies to web        (ports 80/443)
  web       → gunicorn + Django (WhiteNoise static)         (internal)
  worker    → drains the Notification queue + runs solver jobs
  cron      → fires reminder management commands on schedule
  db        → Postgres, persistent named volume + backup cron
```

All app containers share one image (built once); `web`, `worker`, and `cron`
differ only by entrypoint/command. Caddy and Postgres are stock images.
Configuration is via env (`.env`), including `DATABASE_URL`, `SITE_URL`,
`SECRET_KEY`, email + Resend keys, and the public domain for Caddy.

> **Build-time check:** Django 6's Tasks framework is the preferred queue. If a
> chosen backend isn't suitable, the fallback is plain **cron + management
> commands** for every send (reminders are cron-driven regardless), with
> change/preference emails enqueued to a DB table drained by a worker command.
> Either way, no request ever sends 300 emails synchronously.

---

## 3. Data Model

Teams links stay **tied to timeslots**. A timeslot is a fixed
(time + room + link) triple imported from the meeting pool. Assigning or moving
an interview reassigns its timeslot; the link follows automatically.

Existing models (`User`, `Timeslot`, `Interview`, `InterviewPanelist`,
`RescheduleRequest`) are kept. **New models are marked (new).** `Interview` keeps
its `interviewee` and fixed `panelists`; what the solver decides is the
`timeslot` (made nullable until scheduled).

```
User (AbstractUser)
  email (unique), name, role ∈ {admin, interviewer, interviewee}, is_active

Timeslot
  slot_ref (unique), day_label, start_at, end_at, room_label, teams_link
  # imported from the bulk Teams xlsx (the meeting pool / inventory)

Interview
  external_id (unique), title, status ∈ {scheduled, cancelled, completed}
  interviewee  (FK → User, role=interviewee)
  panelists    (M2M → User via InterviewPanelist)   # fixed panel composition
  timeslot     (FK → Timeslot, NULLABLE)            # null until scheduled  ← change
  created_at, updated_at

InterviewPanelist
  interview (FK), user (FK, role=interviewer)        # who examines, not when

PreferenceRound (new)
  name, opens_at, closes_at, status ∈ {draft, open, closed}
  min_available_slots   # int, enforced on submit (0 = no minimum)
  # single round for the event by default; model supports more

PanelistAvailability (new)
  round (FK), panelist (FK → User, role=interviewer), timeslot (FK → Timeslot)
  state ∈ {available, preferred, unavailable}
  (unique together: round, panelist, timeslot)
  # absence of a row = "no response yet"; UI offers day/half-day bulk toggles

SchedulePublication (new)
  created_by (FK → User), created_at, solver_status, objective_value, notes
  # one row per publish; lets us diff old→new and audit who published when

Notification (new)
  recipient (FK → User)
  kind ∈ {schedule_change, preference_request, preference_reminder,
          session_reminder, reschedule_outcome}
  interview (FK, nullable), subject, body
  status ∈ {queued, sent, failed}
  dedupe_key (unique)        # idempotency: e.g. "reminder:<interview>:<user>"
  created_at, sent_at, error

RescheduleRequest
  interview (FK), requested_by (FK), reason
  status ∈ {pending, scheduled, rejected, withdrawn}   # 'forwarded' dropped
  resolution_note, created_at, updated_at              # outcome is internal now

HistoricalInterview        # django-simple-history, automatic
```

Indexes: `Timeslot.start_at`, `Interview.timeslot`, `Interview.interviewee`,
`InterviewPanelist.user`, `PanelistAvailability(round, panelist)`,
`Notification.status`.

> **Availability granularity — locked: per-timeslot.** Panelists mark
> individual timeslots `available`/`preferred`. The form presents a day /
> half-day grid with "select all / clear" bulk toggles so a panelist isn't
> ticking hundreds of boxes one by one, but the stored unit is the timeslot.
> Submission enforces `PreferenceRound.min_available_slots` (see §5).

---

## 4. Scheduling Engine

The core new capability. Given the fixed panel of each interview and the
collected panelist availability, assign each interview to a timeslot.

**Model (OR-Tools CP-SAT):**
- Decision var `x[i,t] ∈ {0,1}` — interview `i` placed in timeslot `t`.
- **Each interview placed once:** `Σ_t x[i,t] = 1` (or = 1 only for
  `status=scheduled`; cancelled ones excluded).
- **One interview per timeslot:** `Σ_i x[i,t] ≤ 1` (a slot = one room at one
  time; parallel sessions are *different* timeslots).
- **Availability:** `x[i,t] = 0` if any panelist of `i` is `unavailable` at `t`
  (or has no `available/preferred` row for `t`).
- **No panelist double-booking:** for each panelist `p` and each wall-clock
  window `w`, `Σ x[i,t] ≤ 1` over interviews `i` containing `p` and timeslots
  `t` overlapping `w`. (Timeslots are grouped by overlap; parallel rooms share a
  window.)
- **Objective (soft):** maximise `preferred` placements, then minimise each
  panelist's day-spread / idle gaps. Tunable weights.

**Workflow:** admin triggers a solve → solver runs as a background task →
produces a *proposed* assignment → admin reviews (incl. any unplaceable
interviews and why) → **publish**. Publish writes the timeslots, records a
`SchedulePublication`, diffs against the previous live assignment, and enqueues
`schedule_change` notifications only for participants whose slot actually
changed.

> Fallback if OR-Tools is unwanted: greedy placement (most-constrained interview
> first) + local-search repair. More code, less optimal, no extra dependency.

---

## 5. Preference Collection

1. Admin creates/opens a `PreferenceRound` (sets `closes_at`).
2. App emails every panelist a `preference_request` with a magic-link to their
   availability form.
3. Panelist marks each slot `available` / `preferred` / leaves blank, using
   day/half-day bulk toggles; saved via htmx. Submission is rejected with a
   clear message ("please mark at least N timeslots") until they meet
   `round.min_available_slots`. A live counter shows progress toward the minimum.
4. App emails `preference_reminder` to non-responders on a schedule (cron) until
   the round closes.
5. Admin dashboard shows response rate; once satisfied, admin runs the solver
   (§4).

---

## 6. Notification Engine

A single `Notification` table is the queue + audit log. Every send goes through
it; `dedupe_key` guarantees idempotency (safe to re-run a cron command).

| kind | trigger | recipients |
|---|---|---|
| `preference_request` | round opened | all panelists |
| `preference_reminder` | cron, while round open | non-responding panelists |
| `schedule_change` | publish (§4) or admin/reschedule edit | participants whose slot changed |
| `session_reminder` | cron (e.g. day-before & 1h-before) | both participants of upcoming sessions |
| `reschedule_outcome` | request resolved | the requester |

- A worker (Tasks framework or `manage.py send_pending_notifications`) drains
  `queued` rows, sends via the configured email backend, marks `sent`/`failed`.
- Time-based kinds (`*_reminder`) are produced by cron-run management commands
  that create `queued` rows (deduped), then drained by the same worker.
- Templates live in `templates/emails/` (text + optional HTML).

---

## 7. URL Routes (user-facing)

| Method | Path | View | Access |
|---|---|---|---|
| GET | `/` | Dashboard: my sessions / login | all |
| GET | `/login`, `/auth/<token>/`, `/logout` | Magic-link auth | public / signed-in |
| GET | `/schedule/` | Full schedule, filterable by day/person | signed-in |
| GET | `/schedule/<id>/` | Session detail (panel, time, Teams link) | signed-in |
| POST | `/schedule/<id>/reschedule/` | File reschedule request | participants |
| GET | `/requests/` | My reschedule requests + status | signed-in |
| GET/POST | `/availability/<round>/` | Panelist availability grid (htmx) | panelists |
| GET | `/admin/` | Django admin: rounds, solve, publish, CRUD | admin |

Admin-side scheduling actions (run solver, review proposal, publish) live as
custom admin views / actions rather than separate public routes.

---

## 8. Imports

- **Teams meeting pool** → `Timeslot` rows. Source is
  `bulkIRPTeamsMeetings_source.xlsx`, sheet `Meetings`
  (`MeetingID, Date, DayLabel, StartTime, EndTime, JoinUrl, Status`). Importer
  exists: `manage.py import_timeslots`. Only rows with `Status=Created` and a
  `JoinUrl` get a live link.
- **Interviews + panels** → `Interview` + `InterviewPanelist`. Importer exists:
  `manage.py import_interviews` (xlsx, sheet `Interviews`, columns
  `ExternalID, Title, IntervieweeEmail, PanelistEmails`; `PanelistEmails` is
  semicolon-separated). No time — the solver assigns that. Role-validated
  (interviewee/interviewer), idempotent by `ExternalID`, `--dry-run`; aborts with
  a per-row error report on unknown/wrong-role email or duplicate interviewee.

---

## 9. Visibility & Authorization

- Signed-in users see the full schedule; **Teams links never shown to the public**.
- Only a session's participants see its "Request reschedule" button.
- Only panelists see/fill their own availability form.
- Admins run rounds, the solver, publishing, and all CRUD; edits are audited
  (user + timestamp + diff).

---

## 10. Phased Build Order

Done so far: scaffold, custom `User` + roles, magic-link auth, base data model
(`Timeslot/Interview/InterviewPanelist/RescheduleRequest`), admin registration,
and the Teams-pool importer.

1. **Container foundation + pipeline** — multi-stage `Dockerfile` (uv-based),
   `compose` with `web` (gunicorn) + `db` (Postgres on a volume); switch dev to
   Postgres; run the existing app in-container. Then stand up the **GitHub
   Actions build→test→deploy pipeline (§11)** so every push to `main` ships to
   the dev VM from the first feature onward. Worker/cron/Caddy services added as
   the subsystems that need them land.
2. **Make `Interview.timeslot` nullable** + migration; interviews can exist
   unscheduled.
3. **Interview/panel importer** (§8).
4. **Dashboard + schedule views** — my sessions, full schedule, session detail
   with the correct Teams link.
5. **Preference round + availability grid** — models, magic-linked form, htmx
   bulk toggles + min-slot enforcement, admin response dashboard.
6. **Notification engine + worker container** — `Notification` model, worker
   service draining the queue, email templates, dedupe; wire `preference_request`
   + add the `cron` service for `preference_reminder`.
7. **Scheduling engine** — OR-Tools model, background solve (worker), admin
   review-and-publish, diff → `schedule_change` notifications.
8. **Session reminders** — cron command producing `session_reminder` rows.
9. **Reschedule flow** — form, participant guard, admin resolution,
   `reschedule_outcome` email.
10. **Harden & ship the full stack** — `django-simple-history`, admin filters,
    add the **Caddy** service (automatic HTTPS), verify the Resend domain
    (SPF/DKIM), Postgres backup cron, and confirm the pipeline deploys the full
    five-service stack.

---

## 11. CI/CD Pipeline

GitHub Actions, one workflow (`.github/workflows/ci-deploy.yml`), three jobs.
Registry: **GHCR** (`ghcr.io/<owner>/<repo>`). Deploy target: the **dev VM**,
reached by the GitHub-hosted runner over **SSH**. Trigger: **push to `main`**
deploys; build+test also run on pull requests, but only `main` deploys.

```
push / PR ─► test ─► build & push ─► deploy
              │         │ (main)       │ (main)
              ▼         ▼              ▼
        test + migrate  GHCR         ssh VM: login, compose pull,
        on a Postgres   :sha+:latest  migrate, up -d
        service container
```

**1. `test`** (every push + PR)
- Postgres service container; `DATABASE_URL` points at it.
- `uv sync`, `manage.py makemigrations --check`, `migrate`, `test` (+ `ruff`).
  Red tests block the pipeline.

**2. `build`** (main only, needs `test`)
- Buildx → log in to GHCR with the built-in `GITHUB_TOKEN` (`packages: write`) →
  build the one app image → push `:${{ github.sha }}` (immutable, for rollback)
  and `:latest`. Layer cache via Actions cache.

**3. `deploy`** (main only, needs `build`)
- Scoped to a GitHub **Environment `dev`** that holds the SSH secrets.
- SSH into the VM (`SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY`[, `SSH_PORT`]) and,
  in `/opt/irp-scheduler`: log in to GHCR with the run's `GITHUB_TOKEN`, then
  `docker compose pull` → `docker compose run --rm web manage.py migrate
  --noinput` → `docker compose up -d` → `docker logout ghcr.io`.
- `collectstatic` runs in the image entrypoint. Compose pins
  `image: ${IMAGE_NAME}:${IMAGE_TAG:-latest}`, so rollback = re-run an older SHA.

**Secrets / variables split**
- *GitHub variables* (non-secret): `SSH_HOST`, `SSH_USER`, `SSH_PORT`,
  `DEPLOY_PATH`.
- *GitHub secret*: `SSH_PRIVATE_KEY` only. The registry uses the automatic
  `GITHUB_TOKEN` — no Docker registry secret.
- *VM `.env`* (app runtime, never in GitHub): `SECRET_KEY`, `RESEND_API_KEY`,
  `POSTGRES_PASSWORD`, `DATABASE_URL`, `SITE_URL`, `ALLOWED_HOSTS`, Caddy domain.

**One-time VM provisioning** (manual, documented): install Docker + compose,
create the deploy user + SSH key, place `docker-compose.yml` + `.env`, point DNS
at the VM for Caddy. The GHCR package is private and linked to the repo; the
deploy job authenticates the pull with the short-lived run token. The pipeline
assumes this exists.

---

## 12. Decisions Locked / Open

**Locked**
- App owns scheduling; OR-Tools CP-SAT solver with admin review-and-publish.
- Availability collected from **panelists only**, **per-timeslot**, with a
  configurable **minimum number of available slots** per round.
- Teams meetings pre-created and imported; **no Graph API**.
- All four email kinds in scope; Notification table is queue + audit; idempotent.
- **Email transport: Resend** (SMTP, verified domain + SPF/DKIM); console only
  for local dev.
- **Deployment: full self-contained `docker compose` stack** — web + worker +
  cron + **Postgres (in a container, named volume)** + **Caddy (automatic
  HTTPS)**. Built Docker-first, develop in-container. Caddy was brought forward
  from Phase 10; the dev VM uses the Azure domain
  `irp-scheduler.uksouth.cloudapp.azure.com` (Caddy behind the `prod` profile,
  `web` bound to loopback).
- **CI/CD: GitHub Actions → GHCR → SSH deploy to the dev VM** (§11). Registry
  uses the built-in `GITHUB_TOKEN` (no Docker registry secret). Build+test on
  every push/PR; **auto-deploy on push to `main`**; image tagged by commit SHA
  for rollback.
- **VM SSH access: key-only (passwords disabled), a dedicated low-privilege
  deploy user, and fail2ban.** Port is publicly reachable (no GitHub IP
  allowlist); Tailscale not used.
- Single-tenant; DB archived after the event.

**Open (confirm before/while building)**
- The actual `min_available_slots` value per round.
- Reminder cadence (e.g. day-before + 1h-before?) and preference-reminder
  frequency.
- Solver objective weights (preference satisfaction vs compactness).
- Background queue: Django 6 Tasks framework vs cron + DB-drain (§2).

---

## 13. Risks

- **Email deliverability** — magic links / notices landing in spam locks people
  out. Mitigation: verified domain + SPF/DKIM via Resend; admin-triggered login
  URL fallback; never send 300 mails in one request.
- **CI deploy access** — GitHub-hosted runners come from a broad, shifting IP
  range, so the VM's SSH can't be allowlisted to "just GitHub" and stays
  publicly reachable. Mitigation (locked): key-only SSH (passwords disabled), a
  dedicated low-privilege deploy user, and fail2ban.
- **Resend volume cap** — the free tier (100/day) is exceeded by one
  schedule-publish to ~300 people. Mitigation: paid tier, or have the worker
  throttle sends across the daily cap.
- **Infeasible schedule** — too little availability to place every interview.
  Mitigation: solver reports unplaceable interviews + the binding constraint;
  admin can reopen the round or relax a panel before publishing.
- **Notification storms / duplicates** — re-running a cron command or a republish
  re-sending mail. Mitigation: `dedupe_key`; change emails fire only on a real
  slot diff.
- **xlsx drift** — upstream meeting-pool format changes silently. Mitigation:
  strict header check + dry-run (already in the importer).
```