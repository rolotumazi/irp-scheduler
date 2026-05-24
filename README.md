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
docker compose exec web python manage.py import_interviews <path-to.xlsx> [--dry-run]
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
| `EMAIL_HOST` | empty | SMTP host (e.g. `smtp.resend.com`). Empty → console backend. |
| `EMAIL_PORT` / `EMAIL_USE_TLS` | `587` / `True` | SMTP transport. |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | empty | For Resend: `resend` / your API key. |
| `EMAIL_BACKEND` | auto | SMTP if `EMAIL_HOST` set, else console. Override only if needed. |
| `DEFAULT_FROM_EMAIL` | `noreply@schedule-irp.local` | From address (must match a verified domain in prod). |
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | `irp` | Used by the `db` service. |
| `IMAGE_NAME` / `IMAGE_TAG` | `schedule-irp` / `latest` | Image ref for prod pulls. |
| `WEB_PORT` | `8000` | Host port (bound to `127.0.0.1` only). |
| `COMPOSE_PROFILES` | empty | Set to `prod` on the VM to start Caddy. |
| `CADDY_DOMAIN` | empty | Public hostname Caddy gets HTTPS for. |
| `SECURE_COOKIES` | `False` | Set `True` once behind HTTPS (secure session/CSRF cookies). |

## Email & notifications

Automated email goes through a `Notification` table (queue + audit, idempotent via
`dedupe_key`). The **`worker`** compose service runs `manage.py run_worker`, which
loops: enqueue due preference reminders, then send all queued notifications via the
email backend. Magic-link sign-in emails are sent synchronously by the web app.

Relevant commands (also runnable by hand):
```bash
docker compose exec web python manage.py enqueue_preference_requests <round_id>
docker compose exec web python manage.py enqueue_preference_reminders
docker compose exec web python manage.py send_notifications
```
(Or use the **"Queue preference-request emails"** action on `PreferenceRound` in the admin.)

**Resend setup.** Add to the VM `.env`:
```ini
EMAIL_HOST=smtp.resend.com
EMAIL_HOST_USER=resend
EMAIL_HOST_PASSWORD=<your-resend-api-key>
DEFAULT_FROM_EMAIL=Schedule IRP <noreply@your-verified-domain>
```
> **Domain caveat:** Resend only delivers to arbitrary recipients from a **domain you
> verify** (SPF/DKIM). The Azure `*.cloudapp.azure.com` name can't be verified, so for
> testing use Resend's `onboarding@resend.dev` (to your own account email only) or a
> Mailtrap sandbox (`EMAIL_HOST=sandbox.smtp.mailtrap.io`).

## CI/CD

`.github/workflows/ci-deploy.yml` — three jobs:

1. **test** (push to `main` + PRs): `ruff`, `makemigrations --check`, `manage.py test` against a Postgres service.
2. **build** (`main` only): build the image and push `:latest` and `:<sha>` to **GHCR** (`ghcr.io/<owner>/<repo>`).
3. **deploy** (`main` only): `scp` the current `docker-compose.yml` + `Caddyfile` to the VM, then SSH in for `compose pull → migrate → up -d`. Rollback = redeploy an older `<sha>`.

The image registry is **GitHub Container Registry**. Both build (push) and deploy
(pull) authenticate with the workflow's built-in `GITHUB_TOKEN`, so **no Docker
registry secrets are required**.

### Required GitHub configuration

Deploy reads connection details from **repository variables** and only the key
from a **secret**:

| Variable | Example |
|---|---|
| `SSH_HOST` | `vm.example.ac.uk` |
| `SSH_USER` | `deploy` |
| `SSH_PORT` | `22` |
| `DEPLOY_PATH` | `/opt/irp-scheduler` |

| Secret | Notes |
|---|---|
| `SSH_PRIVATE_KEY` | private key for the deploy user |

`GITHUB_TOKEN` is provided automatically (image push + pull). Deploy runs under a
GitHub **Environment named `dev`** — create it and (optionally) add a required
reviewer. The GHCR package starts **private** and linked to the repo; the deploy
job logs the VM in to `ghcr.io` with the run token just long enough to pull, so
no long-lived registry credential lives on the VM.

### One-time VM provisioning

The pipeline assumes the dev VM already has:

- Docker + Docker Compose installed.
- A dedicated low-privilege **deploy user** whose public key matches `SSH_PRIVATE_KEY`.
- **Key-only SSH** (passwords disabled) and **fail2ban** — the SSH port is
  publicly reachable because GitHub-hosted runners use a broad IP range.
- `/opt/irp-scheduler/` containing a production `.env` (real `SECRET_KEY`,
  `DATABASE_URL`, `ALLOWED_HOSTS`, `SITE_URL`, `POSTGRES_PASSWORD`,
  `COMPOSE_PROFILES=prod`, `CADDY_DOMAIN`, `SECURE_COOKIES=True`, and—once
  wired—Resend credentials). The deploy job syncs `docker-compose.yml` and
  `Caddyfile`, so only `.env` is placed by hand.
- DNS pointed at the VM (Azure's `*.cloudapp.azure.com` name works).

### HTTPS (Caddy)

Caddy runs behind the `prod` compose profile and gets an automatic Let's Encrypt
certificate for `CADDY_DOMAIN`. To enable it on the VM, the `.env` adds:

```ini
COMPOSE_PROFILES=prod
CADDY_DOMAIN=irp-scheduler.uksouth.cloudapp.azure.com
SECURE_COOKIES=True
ALLOWED_HOSTS=irp-scheduler.uksouth.cloudapp.azure.com
SITE_URL=https://irp-scheduler.uksouth.cloudapp.azure.com
CSRF_TRUSTED_ORIGINS=https://irp-scheduler.uksouth.cloudapp.azure.com
```

Requires NSG inbound **80** and **443** (80 is needed for the ACME challenge and
the HTTP→HTTPS redirect). Caddy proxies to the internal `web` service; `web` is
bound to `127.0.0.1` only, so it's never exposed directly. Certificates persist
in the `caddy_data` volume.
