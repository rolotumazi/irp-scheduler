# Schedule IRP — Platform Plan

A dashboard showing the schedule of ~300 student project presentations ("interviews") over 8 working days. Interviewers, interviewees, and admins log in to view sessions and their Microsoft Teams links. The schedule itself is produced and maintained by external services; this platform is a thin, read-mostly surface over that source of truth.

---

## 1. Goals & Non-Goals

### Goals (v1)
- Provide a single dashboard where all participants see their upcoming sessions and Teams links.
- Let admins oversee, edit, and re-import the full schedule.
- Let participants file a reschedule request, which is forwarded to an external scheduler.
- Accept authoritative schedule updates from the external scheduler via API.

### Non-goals (v1, explicit deferrals)
- Email notifications (send invites, reminders, change notices).
- `.ics` calendar file download.
- Google / Outlook calendar sync.
- In-app chat, comments, or feedback on presentations.
- Self-serve availability collection (handled by upstream app).

---

## 2. Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12+ | Team familiarity |
| Framework | Django 5.x | Free admin site, built-in auth, batteries included |
| Database | SQLite (dev) → Postgres (prod, if multi-instance) | Scale is modest |
| Auth | `django-sesame` magic-link (email) | No passwords for a 2-week event |
| Templates | Django templates + `htmx` for interactions | No SPA needed at this scale |
| Styling | Tailwind via `django-tailwind` (or plain CSS) | Fast to iterate |
| Audit | `django-simple-history` on `Interview` | Track admin edits |
| Email (dev) | Mailtrap | Sandboxes magic-link emails |
| Email (prod) | Resend (free: 3k/month, 100/day) | Clean DX, sufficient volume |
| Hosting | Self-hosted Linux VM, Docker container | Full control, data stays on infra you manage |
| Process manager | Gunicorn behind a reverse proxy (nginx or Caddy) on the host | Standard Django prod setup; reverse proxy handles TLS |
| Static files | WhiteNoise (served from inside the container) | Avoids needing the reverse proxy to know about the app's static dir |
| Timezone | `Europe/London` everywhere | Only supported zone |

---

## 3. Data Model

Teams links are **tied to timeslots, not to interviews** — a timeslot is a fixed (time + room + link) triple created once up front. Rescheduling an interview means reassigning it to a different timeslot; the Teams link follows automatically.

```
User (Django's AbstractUser)
  id, email (unique), name, role ∈ {admin, interviewer, interviewee}, is_active

Timeslot
  id
  start_at, end_at         # tz-aware, stored UTC, displayed Europe/London
  teams_link               # static URL, manually created, bulk-loaded
  room_label               # optional, human-readable ("Room A", "Online-1")
  (unique together: start_at, room_label)

Interview
  id
  external_id              # key used by upstream scheduler for idempotent upsert
  title                    # presentation title
  timeslot                 # FK → Timeslot
  interviewee              # FK → User (role=interviewee)
  status ∈ {scheduled, cancelled, completed}
  created_at, updated_at

InterviewPanelist           # M2M between Interview and interviewers
  interview (FK), user (FK, role=interviewer)
  (unique together: interview, user)

RescheduleRequest
  id
  interview (FK)
  requested_by (FK → User)
  reason                   # free text
  status ∈ {pending, forwarded, accepted, rejected, withdrawn}
  external_ref             # id returned by the external scheduler
  created_at, updated_at

HistoricalInterview         # provided by django-simple-history automatically
```

Indexes: `Timeslot.start_at`, `Interview.timeslot`, `Interview.interviewee`, `InterviewPanelist.user`.

---

## 4. URL Routes (user-facing)

| Method | Path | View | Access |
|---|---|---|---|
| GET | `/` | Dashboard (signed-in: my sessions; signed-out: login) | all |
| GET | `/login` | Request magic link form | public |
| GET | `/auth/<token>/` | Consume magic link | public |
| GET | `/logout` | Logout | signed-in |
| GET | `/schedule/` | Full schedule (all sessions, filterable by day/person) | signed-in |
| GET | `/schedule/<id>/` | Session detail (title, panel, times, Teams link) | signed-in |
| POST | `/schedule/<id>/reschedule/` | Submit reschedule request | participants only |
| GET | `/requests/` | My reschedule requests | signed-in |
| GET | `/admin/` | Django admin (full CRUD, CSV import action) | admin only |

---

## 5. External-facing API (service-token auth)

All endpoints under `/api/v1/`, auth via `Authorization: Bearer <token>` matched against a single static token loaded from the `SCHEDULER_API_TOKEN` env var. JSON in, JSON out. Rotation = update env var + restart container + notify scheduler service.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/schedule/bulk` | Full upsert — idempotent by `external_id` |
| PATCH | `/api/v1/schedule/<external_id>` | Update a single session |
| DELETE | `/api/v1/schedule/<external_id>` | Cancel a session |
| POST | `/api/v1/reschedule/<request_id>/outcome` | Scheduler reports accepted / rejected |

### Outbound webhook
- `POST {SCHEDULER_WEBHOOK_URL}/reschedule-requests` — sent when a user files a reschedule request. Payload: request id, interview external_id, requester email, reason.

---

## 6. Visibility & Authorization

- **Everyone signed in** can see the full schedule (sessions, titles, participants).
- **Only participants** of a session (interviewee or any panelist) see the "Request reschedule" button on that session.
- **Admins** can edit any session and see audit history; admin edits are logged with user + timestamp + diff.
- **Teams links** are visible to signed-in users only (never exposed to the public).

---

## 7. CSV Format (initial import)

Bulk upload is **two files**, loaded in order.

**`timeslots.csv`** — the fixed set of (time, room, Teams link) triples, created once.
```
slot_ref,start_at,end_at,room_label,teams_link
A-0900,2026-05-04T09:00,2026-05-04T09:30,Room A,https://teams.microsoft.com/l/meetup-join/...
B-0900,2026-05-04T09:00,2026-05-04T09:30,Room B,https://teams.microsoft.com/l/meetup-join/...
```

**`interviews.csv`** — assigns presentations to timeslots.
```
external_id,title,slot_ref,interviewee_email,panelist_emails
IRP-001,Quantum foo in bar,A-0900,ada@uni.ac.uk,grace@uni.ac.uk;alan@uni.ac.uk
```

- Times are London local, ISO 8601, no offset.
- `panelist_emails` is semicolon-separated.
- `slot_ref` must exist in the previously-loaded `timeslots.csv`.
- Users referenced by email must already exist; importer aborts on unknown email or missing slot and reports which rows failed.

---

## 8. Phased Build Order

1. **Scaffold** — `django-admin startproject`, add app `interviews`, configure settings, Postgres toggle, London TZ, base template.
2. **User model** — custom User with `role` field; create superuser; load a fixture of test users.
3. **Magic-link auth** — wire `django-sesame`, login request page, email backend (Mailtrap in dev).
4. **Data model** — `Interview`, `InterviewPanelist`, `RescheduleRequest`; migrations; register in Django admin.
5. **CSV import** — management command + admin action; validation & error report.
6. **Dashboard views** — "My sessions" (home), full schedule list, session detail; basic Tailwind styling.
7. **Reschedule flow** — form, participant-only guard, outbound webhook with retry, "My requests" page.
8. **External API** — service token model, the four endpoints above, minimal test suite.
9. **Audit & polish** — `django-simple-history`, admin filters for day/person, swap email to Resend.
10. **Containerise & deploy** — write `Dockerfile` and `docker-compose.yml` (web + Postgres), set up reverse proxy (nginx or Caddy) on the VM with TLS, define backup cron for Postgres volume.
11. **Deferred (post-v1)** — email notifications (schedule published, reminders, reschedule outcomes), `.ics` download, calendar sync.

Estimated effort: 1–2 focused weeks for steps 1–9.

---

## 9. Decisions Locked

- **Tenancy:** Single-tenant. No `Cohort` table. Database is archived after the event.
- **Panel size:** Up to 4 panelists per session. List view designed for 4-across; data model still supports any count.
- **Data retention:** No automated purging. After the event ends, the database is archived in full.
- **Deployment:** Self-managed Linux VM, Docker container, Postgres in a sibling container, reverse proxy on the host for TLS.
- **Service-token auth:** Single static bearer token in `SCHEDULER_API_TOKEN` env var.

---

## 10. Risks

- **Email deliverability** — magic links landing in spam would lock users out. Mitigation: use a verified domain with Resend (SPF/DKIM), and fall back to an admin-triggered login URL.
- **CSV drift** — upstream format changes silently. Mitigation: strict schema validation, header check, dry-run mode.
- **Webhook failures** — external scheduler unreachable when a reschedule is filed. Mitigation: queue requests locally, retry with backoff, surface "pending" state to user.
