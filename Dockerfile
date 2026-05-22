# syntax=docker/dockerfile:1

# ---- builder: resolve dependencies into a self-contained virtualenv ----
FROM python:3.13-slim AS builder

# uv binary, pinned to the version used locally.
COPY --from=ghcr.io/astral-sh/uv:0.7.18 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies only (not the project itself — it's a non-packaged Django
# app that runs straight from source). Cached on the lockfile so source changes
# don't bust the dependency layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --frozen --no-install-project --no-dev

# ---- runtime ----
FROM python:3.13-slim

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=schedule_irp.settings

WORKDIR /app

# Prebuilt virtualenv first (rarely changes), then application source.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app . /app

# WORKDIR created /app as root; hand the dir and the static target to the app
# user so collectstatic can write at runtime.
RUN mkdir -p /app/staticfiles && chown app:app /app /app/staticfiles

USER app
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "schedule_irp.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
