#!/bin/sh
set -e

# Run release tasks only when starting the web server, so one-off commands
# (e.g. `docker compose run web python manage.py test`) and the worker/cron
# containers don't re-migrate or collect static. Both tasks are idempotent.
if [ "$1" = "gunicorn" ]; then
    python manage.py migrate --noinput
    python manage.py collectstatic --noinput
fi

exec "$@"
