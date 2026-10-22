"""Static assertions about the shipped stack files (no Docker daemon needed).

Optional external validators run when available and are skipped otherwise:
``docker compose config`` (the real compose parser, works without a daemon) and
``shellcheck`` (installed by the ``dev`` extra via shellcheck-py).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from my_docker import dockerfile, nginx
from my_docker.checks import resolved_compose
from my_docker.envfile import PLACEHOLDER, env_dict
from my_docker.interpolation import find_refs, walk_strings
from my_docker.project import load_project

CORE = {"app", "db", "mailpit"}
PROFILES = {
    "pgadmin": "tools",
    "redis": "worker",
    "celery": "worker",
    "frontend": "frontend",
    "nginx": "proxy",
    "vault": "vault",
}
SECRET_KEYS = {"POSTGRES_PASSWORD", "DATABASE_PASSWORD", "PGADMIN_DEFAULT_PASSWORD", "VAULT_DEV_ROOT_TOKEN"}


@pytest.fixture
def compose(repo):
    return resolved_compose(load_project(repo, env_file=repo / "example.env"))


@pytest.fixture
def raw_compose(repo):
    return yaml.safe_load((repo / "docker-compose.local.yml").read_text())


def test_services_and_profiles(compose):
    assert {s.name for s in compose.active()} == CORE
    for name, profile in PROFILES.items():
        assert compose.services[name].profiles == (profile,)
    assert set(compose.services) == CORE | set(PROFILES)


def test_no_obsolete_version_key(raw_compose):
    assert "version" not in raw_compose


def test_every_published_port_is_loopback_only(compose):
    for svc in compose.services.values():
        for port in svc.ports:
            assert port.host_ip == "127.0.0.1", f"{svc.name} publishes {port} on all interfaces"


def test_published_ports_are_configurable(raw_compose):
    for name, svc in raw_compose["services"].items():
        for port in svc.get("ports", []):
            assert "${" in port, f"{name}: host port {port} is hardcoded"


def test_all_images_pinned(compose):
    for svc in compose.services.values():
        if svc.image:
            assert not dockerfile.is_unpinned(svc.image), svc.image


def test_backend_services_share_the_backend_network(compose):
    for name in ("app", "db", "mailpit", "pgadmin", "redis", "celery", "vault", "nginx"):
        assert "backend" in compose.services[name].networks, name
    assert set(compose.services["nginx"].networks) == {"frontend", "backend"}


def test_example_env_matches_compose_references(repo, raw_compose):
    example = env_dict((repo / "example.env").read_text())
    referenced = {r.name for text in walk_strings(raw_compose) for r in find_refs(text)}
    assert referenced == set(example), (
        f"missing in example.env: {referenced - set(example)}, "
        f"unused: {set(example) - referenced}"
    )


def test_example_env_has_no_real_secrets(repo):
    example = env_dict((repo / "example.env").read_text())
    for key, value in example.items():
        if any(word in key for word in ("PASSWORD", "TOKEN", "SECRET")):
            assert value == PLACEHOLDER, f"{key} should be '{PLACEHOLDER}' in example.env"
    assert SECRET_KEYS <= set(example)


def test_required_secrets_fail_loudly(raw_compose):
    env = raw_compose["services"]["db"]["environment"]
    for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        assert env[key].startswith(f"${{{key}:?"), key


def test_db_init_script_is_mounted_and_executable(repo, compose):
    db = compose.services["db"]
    mount = next(m for m in db.mounts if m.target.startswith("/docker-entrypoint-initdb.d/"))
    assert mount.read_only and (repo / mount.source).is_file()
    assert os.stat(repo / "init-script.sh").st_mode & stat.S_IXUSR
    assert {"DATABASE_USER", "DATABASE_PASSWORD"} <= set(db.environment)


def test_db_healthcheck_uses_container_env(raw_compose):
    test = raw_compose["services"]["db"]["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL" and "$$POSTGRES_USER" in test[1]


def test_app_dockerfile(repo):
    ins = dockerfile.parse_dockerfile((repo / "Dockerfile").read_text())
    keywords = [i.keyword for i in ins]
    for required in ("WORKDIR", "USER", "EXPOSE", "HEALTHCHECK", "CMD"):
        assert required in keywords
    assert keywords.index("USER") > keywords.index("RUN")  # root only for installs
    values = dockerfile.arg_values(ins, {})
    assert values["TZ"] == "UTC"  # no personal timezone baked in
    assert dockerfile.exposed_ports(ins, values) == {"8000"}
    for source in dockerfile.copy_sources(ins, values):
        assert (repo / source).exists(), source


def test_frontend_dockerfile(repo):
    ins = dockerfile.parse_dockerfile((repo / "Dockerfile_frontend").read_text())
    values = dockerfile.arg_values(ins, {})
    image = dockerfile.base_images(ins, values)[0]
    assert image == "node:20-slim"
    assert dockerfile.exposed_ports(ins, values) == {"8080"}


def test_dockerignore_keeps_secrets_out_of_build_context(repo):
    ignored = (repo / ".dockerignore").read_text().split()
    assert {".env", ".venv", ".git"} <= set(ignored)


def test_nginx_conf(repo, compose):
    text = (repo / "nginx.conf").read_text()
    tree = nginx.parse(text)
    assert nginx.listen_ports(tree) == {"80"}
    assert nginx.upstreams(tree) == {"app_upstream": ["app:8000"]}
    assert nginx.template_vars(text) <= set(compose.services["nginx"].environment)
    assert "yourdomain.com" not in text and "/path/to/" not in text
    alias = next(nginx.iter_directives(tree, "alias")).args[0]
    static_mount = next(m for m in compose.services["nginx"].mounts if m.source == "static_data")
    assert alias.rstrip("/") == static_mount.target


def test_init_script_bash_syntax(repo):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    subprocess.run([bash, "-n", str(repo / "init-script.sh")], check=True)


def test_init_script_shellcheck(repo):
    # shellcheck-py installs the binary next to the interpreter, which may not be on PATH
    shellcheck = shutil.which("shellcheck") or shutil.which(
        "shellcheck", path=str(Path(sys.executable).parent)
    )
    if not shellcheck:
        pytest.skip("shellcheck not installed (pip install -e '.[dev]')")
    result = subprocess.run([shellcheck, str(repo / "init-script.sh")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout


# --- init-script.sh behaviour with a fake psql --------------------------------------------

FAKE_PSQL = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_PSQL_ARGS"
cat > "$FAKE_PSQL_STDIN"
"""


def run_init(repo: Path, tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    psql = bindir / "psql"
    psql.write_text(FAKE_PSQL)
    psql.chmod(0o755)
    args_file, stdin_file = tmp_path / "args", tmp_path / "stdin"
    full_env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "FAKE_PSQL_ARGS": str(args_file),
        "FAKE_PSQL_STDIN": str(stdin_file),
        **env,
    }
    proc = subprocess.run(
        [bash, str(repo / "init-script.sh")], env=full_env, capture_output=True, text=True
    )
    return proc, args_file, stdin_file


def test_init_script_creates_role_via_psql_variables(repo, tmp_path):
    password = "p'w\"; DROP TABLE x; --"
    proc, args_file, stdin_file = run_init(
        repo, tmp_path,
        POSTGRES_USER="postgres", POSTGRES_DB="appdb",
        DATABASE_USER="app", DATABASE_PASSWORD=password,
    )
    assert proc.returncode == 0, proc.stderr
    args = args_file.read_text().splitlines()
    assert args[:2] == ["-v", "ON_ERROR_STOP=1"]
    assert "app_user=app" in args and f"app_password={password}" in args and "app_db=appdb" in args
    sql = stdin_file.read_text()
    assert password not in sql  # values never spliced into SQL text
    assert "CREATE ROLE %I LOGIN PASSWORD %L" in sql and "\\gexec" in sql
    assert "role 'app' owns database 'appdb'" in proc.stdout


def test_init_script_skips_when_app_user_is_superuser(repo, tmp_path):
    proc, args_file, _ = run_init(
        repo, tmp_path, POSTGRES_USER="postgres", POSTGRES_DB="d", DATABASE_USER="postgres"
    )
    assert proc.returncode == 0 and "nothing to do" in proc.stdout
    assert not args_file.exists()


def test_init_script_requires_password(repo, tmp_path):
    proc, args_file, _ = run_init(repo, tmp_path, POSTGRES_USER="postgres", DATABASE_USER="app")
    assert proc.returncode == 1 and "DATABASE_PASSWORD must be set" in proc.stderr
    assert not args_file.exists()


def test_init_script_requires_postgres_user(repo, tmp_path):
    proc, _, _ = run_init(repo, tmp_path, DATABASE_USER="app", DATABASE_PASSWORD="x")
    assert proc.returncode != 0 and "POSTGRES_USER must be set" in proc.stderr


# --- the real compose parser, when the CLI is installed --------------------------------


def _docker_compose() -> list[str] | None:
    docker = shutil.which("docker")
    if not docker:
        return None
    probe = subprocess.run([docker, "compose", "version"], capture_output=True)
    return [docker, "compose"] if probe.returncode == 0 else None


@pytest.mark.parametrize("profiles", [[], ["*"]])
def test_docker_compose_config_accepts_the_stack(repo, tmp_path, profiles):
    cmd = _docker_compose()
    if cmd is None:
        pytest.skip("docker compose CLI not available")
    args = [*cmd, "-f", str(repo / "docker-compose.local.yml"), "--env-file", str(repo / "example.env")]
    for p in profiles:
        args += ["--profile", p]
    env = {**os.environ, "DOCKER_CONFIG": str(tmp_path)}
    result = subprocess.run([*args, "config", "--services"], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    expected = CORE if not profiles else CORE | set(PROFILES)
    assert set(result.stdout.split()) == expected


def test_docker_compose_config_rejects_missing_secrets(repo, tmp_path):
    cmd = _docker_compose()
    if cmd is None:
        pytest.skip("docker compose CLI not available")
    empty = tmp_path / "empty.env"
    empty.write_text("")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("POSTGRES_", "DATABASE_"))
    } | {"DOCKER_CONFIG": str(tmp_path)}
    result = subprocess.run(
        [*cmd, "-f", str(repo / "docker-compose.local.yml"), "--env-file", str(empty), "config", "-q"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode != 0
    assert "required variable POSTGRES_DB is missing a value" in result.stderr
