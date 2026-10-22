# my_docker

A reusable **local Docker development stack** for Python web projects, plus a small helper
CLI (`my-docker`) that generates a `.env` with random secrets and statically checks the stack
wiring without needing a Docker daemon.

The stack itself stays plain Docker: `docker-compose.local.yml`, two Dockerfiles, `nginx.conf`,
`init-script.sh` and `example.env`. Copy them into a project (or use this repo as a starting
point), put your code in `app/`, and run `docker compose`.

> **Verification note:** this was developed in an environment **without a Docker daemon**. Nothing
> here has been run with `docker compose up` or `docker build`. What *was* verified: the compose
> file is accepted by the real `docker compose config` (v5.6) for the core stack and all profiles,
> `init-script.sh` was run against a real local PostgreSQL 16 (twice, to prove idempotence, with a
> role name and password containing quotes), the placeholder app was run and queried locally, and
> the pytest suite checks the rest statically.

## What's in the stack

| Service    | Profile    | Image / build                     | Host port (default, 127.0.0.1) |
|------------|------------|-----------------------------------|--------------------------------|
| `app`      | core       | `Dockerfile` (Python 3.12 slim)   | 8000                           |
| `db`       | core       | `postgres:16`                     | 5432                           |
| `mailpit`  | core       | `axllent/mailpit` (SMTP catcher)  | 8025 (UI), 1025 (SMTP)         |
| `pgadmin`  | `tools`    | `dpage/pgadmin4:8`                | 5050                           |
| `redis`    | `worker`   | `redis:7-alpine`                  | 6379                           |
| `celery`   | `worker`   | same image as `app`               | -                              |
| `frontend` | `frontend` | `Dockerfile_frontend` (Node 20)   | 8080                           |
| `nginx`    | `proxy`    | `nginx:1.27-alpine`               | 80                             |
| `vault`    | `vault`    | `hashicorp/vault:1.17` (dev mode) | 8200                           |

- **Networks:** `backend` (everything that talks to the database) and `frontend` (the Node dev
  server); nginx sits on both.
- **app** is built from `Dockerfile`: Python + optional dev tool belt (black, flake8, isort, mypy,
  poetry, ansible, ssh/rsync, vim; `INSTALL_DEV_TOOLS=0` to skip), configurable timezone, a
  non-root `developer` user, a `/health` HEALTHCHECK. `app/` holds a stdlib-only placeholder that
  answers `/` and `/health` and reports whether Postgres is reachable. Replace it with your project
  (it needs a `requirements.txt` and a `main.py`, or change `CMD`). The code is bind-mounted, so
  edits show up without a rebuild.
- **db** runs `init-script.sh` on first start (empty volume). If `DATABASE_USER` is not the
  superuser, the script creates that login role and makes it the owner of `POSTGRES_DB`. Values go
  to psql as variables and are quoted with `format(%I/%L)`, and running it again is harmless.
- **nginx** renders `nginx.conf` through the official image's envsubst templating
  (`${NGINX_SERVER_NAME}`, `${NGINX_CLIENT_MAX_BODY_SIZE}`). It proxies to `app:8000` (with
  WebSocket upgrade headers) and serves `/static/` from the `static_data` volume that the app
  writes to `staticfiles/`.

## Install the helper

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'      # or: pip install .   (runtime dependency: PyYAML)
```

## Usage

```bash
my-docker env                                    # example.env -> .env with fresh secrets
my-docker check                                  # static validation, exit 1 on errors
docker compose -f docker-compose.local.yml up --build                  # app + db + mailpit
docker compose -f docker-compose.local.yml --profile tools --profile worker up
my-docker services --profile worker              # what that would start, and on which ports
```

Real output (from a copy of the stack in a scratch directory):

```text
$ my-docker env --set APP_PORT=8001
wrote .env
generated secrets for: POSTGRES_PASSWORD, DATABASE_PASSWORD, PGADMIN_DEFAULT_PASSWORD, VAULT_DEV_ROOT_TOKEN
overridden: APP_PORT

$ my-docker check
warning: [BIND-MISSING] service frontend bind-mounts missing path ./frontend
warning: [BUILD-COPY-MISSING] service frontend: COPY pattern frontend/package*.json matches nothing in .
warning: [BUILD-COPY-MISSING] service frontend: COPY source frontend/ not found in .
docker-compose.local.yml with .env: 0 error(s), 3 warning(s)

$ my-docker services --profile worker
SERVICE  PROFILES  PORTS                                       DEPENDS ON
app      (core)    127.0.0.1:8001->8000                        db
db       (core)    127.0.0.1:5432->5432                        -
mailpit  (core)    127.0.0.1:8025->8025, 127.0.0.1:1025->1025  -
celery   worker    -                                           db, redis
redis    worker    127.0.0.1:6379->6379                        -
profiles: frontend, proxy, tools, vault, worker
```

(The frontend warnings appear because no `./frontend` directory ships. Problems in optional
profiles are reported as warnings and problems in core services as errors.)

The original 2024 prototype is kept as a test fixture, and the checker finds its real bugs:

```text
$ my-docker check -C tests/fixtures/original -q
error: [BUILD-NO-CMD] service app: Dockerfile has no CMD/ENTRYPOINT and the service sets no command, so the container exits immediately
error: [NET-UNREACHABLE] service app depends on db but they share no network
error: [NET-UNREACHABLE] service app points DATABASE_HOST at db but they share no network
error: [NET-UNREACHABLE] service pgadmin depends on db but they share no network
.../docker-compose.local.yml with .../example.env: 4 error(s), 5 warning(s)
```

### Commands

- `my-docker env [-C DIR] [--example FILE] [-o FILE|-] [--set KEY=VALUE]... [--force]`:
  renders `.env` from `example.env`. Every value equal to `change-me` becomes a random 32-character
  secret. Comments and order are preserved. The file is written with mode 0600, the command never
  overwrites without `--force`, and it never prints secret values.
- `my-docker check [-C DIR] [-f FILE] [--env-file FILE] [--strict] [-q]`: uses `DIR/.env` if it
  exists, as compose does, and otherwise `example.env`. Exits 0 when clean, 1 on errors (or on
  warnings with `--strict`), and 2 on usage or load errors.
- `my-docker services [--profile NAME]...`: shows services, profiles, published ports and
  dependencies after resolving `${VAR}`s.

### What `check` verifies

| Code(s) | Meaning |
|---|---|
| `VAR-REQUIRED`, `VAR-UNSET`, `VAR-UNUSED`, `VAR-SYNTAX` | `${VAR}` references vs the env file (compose semantics for `:-`, `-`, `:?`, `?`, `:+`, `+`, `$$`) |
| `ENV-MISSING`, `ENV-PLACEHOLDER` | `.env` lacks keys from `example.env` or still contains `change-me` |
| `NET-UNDECLARED`, `VOL-UNDECLARED`, `BIND-MISSING` | networks/volumes declared, bind-mount sources exist |
| `NET-UNREACHABLE` | a service shares no network with something it depends on, points an env var at (`db`, `redis://redis:6379`), or proxies to |
| `DEP-UNKNOWN`, `DEP-PROFILE`, `DEP-NO-HEALTHCHECK`, `DEP-HEALTH-UNKNOWN` | `depends_on` targets exist, core services don't depend on profiled ones, `service_healthy` targets have a healthcheck (compose or Dockerfile) |
| `PORT-CONFLICT` | two services publish the same host port/interface |
| `BUILD-CONTEXT`, `BUILD-DOCKERFILE`, `BUILD-NO-CMD`, `BUILD-COPY-MISSING`, `IMAGE-UNPINNED` | build context/Dockerfile exist, the image has a CMD, COPY sources exist after build-arg substitution, images are pinned |
| `NGINX-SYNTAX`, `NGINX-TEMPLATE-VAR`, `NGINX-NOT-TEMPLATE`, `NGINX-UPSTREAM`, `NGINX-OPTIONAL-UPSTREAM`, `NGINX-PORT`, `NGINX-LISTEN` | the mounted nginx config parses, template placeholders are provided, upstreams are reachable compose services on an exposed port, published ports are listened on |
| `INITDB-NOT-EXECUTABLE`, `INITDB-ENV` | `docker-entrypoint-initdb.d` scripts are executable and get the env vars they read |

## Configuration

Everything is configured through `.env` (see `example.env`, which documents every key):
credentials (`POSTGRES_*`, `DATABASE_*`, `PGADMIN_*`, `VAULT_DEV_ROOT_TOKEN`), host ports
(`APP_PORT`, `DB_PORT`, `MAIL_UI_PORT`, `MAIL_SMTP_PORT`, `PGADMIN_PORT`, `REDIS_PORT`,
`FRONTEND_PORT`, `NGINX_PORT`, `VAULT_PORT`), image and build settings (`POSTGRES_VERSION`,
`PYTHON_VERSION`, `NODE_VERSION`, `TZ`, `INSTALL_DEV_TOOLS`, `APP_DIR`, `FRONTEND_DIR`,
`FRONTEND_NPM_SCRIPT`), Celery (`CELERY_APP`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`) and
nginx (`NGINX_SERVER_NAME`, `NGINX_CLIENT_MAX_BODY_SIZE`). The core secrets use `${VAR:?}`, so
`docker compose` refuses to start without them.

The app container receives `DATABASE_HOST=db`, `DATABASE_PORT`, `DATABASE_NAME`,
`DATABASE_USER`, `DATABASE_PASSWORD`, `EMAIL_HOST=mailpit`, `EMAIL_PORT=1025`,
`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` and `TZ`.

## Design (helper package, `src/my_docker/`)

- `interpolation.py`: compose `${VAR}` substitution and reference discovery (pure).
- `envfile.py`: env-file parsing, quoting that round-trips, `.env` rendering with secrets (pure).
- `compose.py`, `dockerfile.py`, `nginx.py`: small parsers that normalise compose
  short/long syntax, Dockerfile instructions/ARGs/COPY sources, and the nginx directive tree (pure).
- `project.py`: the only I/O, which loads the stack directory. `checks.py` holds the rules and
  returns `Finding`s. `cli.py` is the argparse front end.

## Tests

```bash
.venv/bin/pytest
```

163 tests cover the parsers and every checker rule. Each rule is tested by mutating a `tmp_path`
copy of the stack. The original prototype is a regression fixture. The suite also includes
static assertions on the shipped files (loopback-only ports, pinned images, `example.env` keys
match the compose references exactly, no real secrets), `init-script.sh` run under bash with a
fake `psql`, `bash -n` and shellcheck (shellcheck comes from the `dev` extra), the placeholder
app's HTTP endpoints, and the CLI. When the `docker` CLI is installed, `docker compose config`
is also run for the core stack and all profiles. It needs no daemon, and the tests skip it when
the CLI is missing. The tests use no network and touch nothing outside `tmp_path`.

## Known limitations

- **Never run against a real Docker daemon** (see the note at the top). Image builds, container
  start-up order, healthchecks, file ownership on the bind mounts and the nginx/pgAdmin/Vault/
  Mailpit containers are untested in practice.
- The `worker` profile assumes your app defines a Celery app importable as `CELERY_APP` and has
  `celery` in `requirements.txt`. The placeholder app does not. The `frontend` profile expects a
  `./frontend` directory with a `package.json`.
- The `app` image runs as uid 1000 (`USER_UID` build arg). If your host uid differs, files the
  container writes into the bind-mounted `app/` may need fixing.
- The `certbot` and Let's Encrypt TLS setup from the prototype was dropped because it can't work
  on a local machine. Vault runs in dev mode (in-memory, unsealed) and is for local use only.
- `my-docker check` models the parts of the compose spec this stack uses. It is not a full
  replacement for `docker compose config`, and it doesn't model `extends`, `include`, multiple `-f`
  files or Dockerfile ARG scoping across stages.
