import os
import stat
import subprocess
import sys

import pytest

from my_docker.cli import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE, main
from my_docker.envfile import PLACEHOLDER, env_dict


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_help_lists_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for command in ("env", "check", "services"):
        assert command in out


def test_missing_command_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_env_generates_secrets(stack, capsys):
    code, out, _ = run(capsys, "env", "-C", str(stack.root), "--set", "NGINX_PORT=8081")
    assert code == EXIT_OK
    env_path = stack.root / ".env"
    env = env_dict(env_path.read_text())
    example = env_dict((stack.root / "example.env").read_text())
    assert set(env) == set(example)
    secrets = ["POSTGRES_PASSWORD", "DATABASE_PASSWORD", "PGADMIN_DEFAULT_PASSWORD", "VAULT_DEV_ROOT_TOKEN"]
    for key in secrets:
        assert env[key] != PLACEHOLDER and len(env[key]) >= 32
    assert len({env[k] for k in secrets}) == len(secrets)
    assert env["NGINX_PORT"] == "8081" and env["POSTGRES_USER"] == "postgres"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert "generated secrets for: POSTGRES_PASSWORD" in out
    for key in secrets:
        assert env[key] not in out  # secrets are never echoed

    # the generated .env passes the checker without placeholder warnings
    code, out, _ = run(capsys, "check", "-C", str(stack.root), "--strict")
    assert code == EXIT_OK, out
    assert ".env: 0 error(s), 0 warning(s)" in out


def test_env_refuses_to_overwrite(stack, capsys):
    (stack.root / ".env").write_text("KEEP=1\n")
    code, _, err = run(capsys, "env", "-C", str(stack.root))
    assert code == EXIT_USAGE and "already exists" in err
    assert (stack.root / ".env").read_text() == "KEEP=1\n"
    code, _, _ = run(capsys, "env", "-C", str(stack.root), "--force")
    assert code == EXIT_OK and "KEEP" not in (stack.root / ".env").read_text()


def test_env_to_stdout(stack, capsys):
    code, out, _ = run(capsys, "env", "-C", str(stack.root), "-o", "-")
    assert code == EXIT_OK and "POSTGRES_USER=postgres" in out
    assert not (stack.root / ".env").exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--set", "no-equals"], "expected KEY=VALUE"),
        (["--example", "missing.env"], "cannot read"),
    ],
)
def test_env_errors(stack, capsys, argv, message):
    code, _, err = run(capsys, "env", "-C", str(stack.root), *argv)
    assert code == EXIT_USAGE and message in err


def test_env_reports_bad_example(stack, capsys):
    (stack.root / "example.env").write_text("NOT VALID\n")
    code, _, err = run(capsys, "env", "-C", str(stack.root))
    assert code == EXIT_USAGE and "line 1" in err


def test_check_clean_stack(stack, capsys):
    code, out, _ = run(capsys, "check", "-C", str(stack.root))
    assert code == EXIT_OK
    assert out.strip().endswith("example.env: 0 error(s), 0 warning(s)")


def test_check_original_prototype_fails(original, capsys):
    code, out, _ = run(capsys, "check", "-C", str(original))
    assert code == EXIT_FINDINGS
    assert "[NET-UNREACHABLE] service app depends on db but they share no network" in out
    code, out, _ = run(capsys, "check", "-C", str(original), "-q")
    assert "warning:" not in out


def test_check_strict_turns_warnings_into_failure(stack, capsys):
    (stack.root / "example.env").write_text((stack.root / "example.env").read_text() + "X=1\n")
    assert run(capsys, "check", "-C", str(stack.root))[0] == EXIT_OK
    assert run(capsys, "check", "-C", str(stack.root), "--strict")[0] == EXIT_FINDINGS


def test_check_explicit_env_file(stack, tmp_path, capsys):
    other = tmp_path / "other.env"
    other.write_text("POSTGRES_USER=u\n")
    code, out, _ = run(capsys, "check", "-C", str(stack.root), "--env-file", str(other))
    assert code == EXIT_FINDINGS and "VAR-REQUIRED" in out


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda root: (root / "docker-compose.local.yml").unlink(), "cannot read compose file"),
        (lambda root: (root / "docker-compose.local.yml").write_text("a: [\n"), "invalid YAML"),
        (lambda root: (root / "example.env").write_text("oops\n"), "example.env: line 1"),
    ],
)
def test_check_load_errors(stack, capsys, setup, message):
    setup(stack.root)
    code, _, err = run(capsys, "check", "-C", str(stack.root))
    assert code == EXIT_USAGE and message in err


def test_check_missing_directory(tmp_path, capsys):
    code, _, err = run(capsys, "check", "-C", str(tmp_path / "nope"))
    assert code == EXIT_USAGE and "not a directory" in err


def test_services_table(stack, capsys):
    code, out, _ = run(capsys, "services", "-C", str(stack.root))
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0].split() == ["SERVICE", "PROFILES", "PORTS", "DEPENDS", "ON"]
    assert lines[1].split()[:3] == ["app", "(core)", "127.0.0.1:8000->8000"]
    assert lines[-1] == "profiles: frontend, proxy, tools, vault, worker"


def test_services_profile_filter_and_env(stack, capsys):
    (stack.root / ".env").write_text(
        (stack.root / "example.env").read_text().replace("APP_PORT=8000", "APP_PORT=9000")
    )
    code, out, _ = run(capsys, "services", "-C", str(stack.root), "--profile", "worker")
    names = [line.split()[0] for line in out.splitlines()[1:-1]]
    assert sorted(names) == ["app", "celery", "db", "mailpit", "redis"]
    assert "127.0.0.1:9000->8000" in out


def test_services_reports_unresolvable_stack(stack, tmp_path, capsys):
    empty = tmp_path / "empty.env"
    empty.write_text("")
    code, _, err = run(capsys, "services", "-C", str(stack.root), "--env-file", str(empty))
    assert code == EXIT_USAGE and "my-docker check" in err


def test_module_entry_point(stack):
    result = subprocess.run(
        [sys.executable, "-m", "my_docker", "check", "-C", str(stack.root)],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    assert result.returncode == 0, result.stderr
