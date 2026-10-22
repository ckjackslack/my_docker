# my_docker

> **Draft** - initial assessment of the original prototype. Not finished yet.

## What this seems to be

A personal, reusable **local Docker development stack** for Python web projects: a
`docker-compose.local.yml` that wires a Python "app" container (built from a dev-tools-heavy
`Dockerfile`) to PostgreSQL, pgAdmin and MailHog, plus a menu of commented-out optional
services (Redis, Celery worker, a Node frontend, nginx + certbot, Vault) to switch on per
project. `example.env` is the template for the `.env` file, `init-script.sh` is meant to run in
Postgres' `docker-entrypoint-initdb.d`, and `nginx.conf` is a reverse-proxy config (HTTP->HTTPS
redirect, Let's Encrypt, proxy to `app:8000`).

## Current state (honest assessment)

Single commit ("intermediate changes"), clearly a work in progress.

What works / is sound:
- The compose file is valid YAML and the general shape (app + db + pgadmin + mail catcher) is right.
- The db healthcheck + `depends_on: condition: service_healthy` idea is right.

What is broken or missing:
- **Networking bug:** `app`, `pgadmin` and `mailhog` are attached to the `backend`/`frontend`
  networks, but `db` declares no networks so it only joins `default`. The app and pgAdmin
  therefore cannot reach the database at all.
- The `app` image has no `WORKDIR`, no copied code and no `CMD`, so the container exits
  immediately (and `depends_on` waiters have nothing to talk to).
- Optional services are toggled by commenting YAML in and out (error prone); compose
  *profiles* are the native mechanism.
- `init-script.sh` is never mounted (the commented mount points at `init_script.sh`, a
  different name) and it hardcodes a `myuser` / `mypassword` role.
- `nginx.conf` uses `yourdomain.com` placeholders, Let's Encrypt paths that cannot work on a
  local machine, and a `/path/to/your/staticfiles/` placeholder; it is not mounted anywhere.
- `Dockerfile_frontend` uses Node 14 (EOL); `postgres:latest` / `mailhog` are unpinned, MailHog
  is unmaintained.
- Hardcoded personal-ish defaults: `TZ=Europe/Warsaw`, Vault root token `myroot`,
  `postgres`/`admin` passwords in `example.env` (dev placeholders, not real secrets).
- Duplicated/inconsistent config: `DATABASE_*` vs `POSTGRES_*` hold the same values twice;
  `CELERY_*` variables are defined but the services using them are commented out.
- Host ports are published on all interfaces and are not configurable.

## TODO / to be finished

- [ ] Redesign the stack with compose profiles, fixed networking and configurable ports.
- [ ] Generate `.env` with random secrets; validate the stack statically (no Docker daemon here).
- [ ] Usage docs, tests.
