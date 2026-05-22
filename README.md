# Schedule IRP

Web app that collects panelist availability, computes the schedule for ~300
student project presentations ("interviews") across 8 working days, and emails
participants their sessions and Microsoft Teams links. See **[PLAN.md](PLAN.md)**
for the full design and phase plan.

> **Status:** Phase 1 complete — the app runs as a containerised stack
> (Django + Postgres) with a GitHub Actions build → test → deploy pipeline.
> Feature work (interview import, dashboard, availability, notifications, the
> solver) follows the phases in `PLAN.md`.

## Stack

Django 6 · Postgres · magic-link auth (django-sesame) · Resend (email) ·
OR-Tools (solver, later) · Docker Compose · Caddy (TLS, later) · GitHub Actions.

## Local development

Everything runs in containers; no local Python setup is required.

```bash
docker compose up --build          # app on http://127.0.0.1:8000
```

Sane defaults are baked into `docker-compose.yml`, so this works with **no `.env`**.
On start the web container migrates and collects static files automatically.

Useful one-offs:

```bash
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py seed_users        # test users (idempotent)
docker compose exec web python manage.py import_timeslots <path-to.xlsx> [--dry-run]
docker compose down                # stop (add -v to also drop the Postgres volume)
```

`seed_users` creates an admin (`admin@example.com` / `adminpass`) plus interviewers
and interviewees (magic-link only). Routes: `/` (dashboard), `/login/`,
`/admin/`, `/healthz` (liveness).

### Without Docker

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py runserver     # uses SQLite unless DATABASE_URL is set
```

## Tests (TDD)

```bash
docker compose run --rm web python manage.py test      # against Postgres
# or, locally:
uv run python manage.py test
```

The CI pipeline runs the same suite plus `ruff` and a `makemigrations --check`
on every push to `main` and every pull request.

## Configuration

Read from the environment (see `schedule_irp/settings.py`). Defaults are
dev-only; set real values via the VM's `.env` in production.

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | insecure dev key | **Set in prod.** |
| `DEBUG` | `False` | `True` only for local dev. |
| `ALLOWED_HOSTS` | empty | Comma-separated; e.g. `schedule.example.ac.uk`. |
| `CSRF_TRUSTED_ORIGINS` | empty | e.g. `https://schedule.example.ac.uk` (needed behind Caddy). |
| `DATABASE_URL` | SQLite file | `postgres://USER:PASS@db:5432/NAME` in the stack. |
| `SITE_URL` | `http://127.0.0.1:8000` | Base URL used in magic-link emails. |
| `EMAIL_BACKEND` | console | Resend SMTP backend wired in with the notification engine (later phase). |
| `DEFAULT_FROM_EMAIL` | `noreply@schedule-irp.local` | From address. |
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | `irp` | Used by the `db` service. |
| `IMAGE_NAME` / `IMAGE_TAG` | `schedule-irp` / `latest` | Image ref for prod pulls. |
| `WEB_PORT` | `8000` | Host port mapping. |

## CI/CD

`.github/workflows/ci-deploy.yml` — three jobs:

1. **test** (push to `main` + PRs): `ruff`, `makemigrations --check`, `manage.py test` against a Postgres service.
2. **build** (`main` only): build the image, push `:latest` and `:<sha>` to Docker Hub.
3. **deploy** (`main` only): SSH into the dev VM and `compose pull → migrate → up -d`. Rollback = redeploy an older `<sha>`.

### Required GitHub secrets

| Secret | Used by |
|---|---|
| `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` | build (also forms the image name `<user>/schedule-irp`) |
| `SSH_HOST`, `SSH_USER`, `SSH_PRIVATE_KEY` | deploy |
| `SSH_PORT` | deploy (optional; defaults to 22) |

Deploy runs under a GitHub **Environment named `dev`** — create it and (optionally)
add a required reviewer there.

### One-time VM provisioning

The pipeline assumes the dev VM already has:

- Docker + Docker Compose installed.
- A dedicated low-privilege **deploy user** whose public key matches `SSH_PRIVATE_KEY`.
- **Key-only SSH** (passwords disabled) and **fail2ban** — the SSH port is
  publicly reachable because GitHub-hosted runners use a broad IP range.
- `/opt/schedule-irp/` containing `docker-compose.yml` and a production `.env`
  (real `SECRET_KEY`, `DATABASE_URL`, `ALLOWED_HOSTS`, `SITE_URL`,
  `POSTGRES_PASSWORD`, and—once wired—Resend credentials).
- DNS pointed at the VM (for Caddy automatic HTTPS, added in a later phase).
