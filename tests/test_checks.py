"""The static checker: clean on the shipped stack, catches the original bugs and mutations."""

import os

import pytest

from my_docker.checks import ERROR, WARNING, errors, run_checks
from my_docker.project import load_project


def codes(findings, level=None):
    return {f.code for f in findings if level is None or f.level == level}


def check(root, **kwargs):
    return run_checks(load_project(root, **kwargs))


def test_shipped_stack_is_clean(repo):
    findings = check(repo, env_file=repo / "example.env")
    assert errors(findings) == []
    # the only warnings are about the optional ./frontend directory, which is not shipped
    assert all("frontend" in f.message for f in findings), findings


def test_stack_with_frontend_dir_has_no_findings(stack):
    assert check(stack.root) == []


def test_original_prototype_bugs_are_detected(original):
    findings = check(original)
    messages = [str(f) for f in errors(findings)]
    assert "error: [NET-UNREACHABLE] service app depends on db but they share no network" in messages
    assert any("points DATABASE_HOST at db" in m for m in messages)
    assert any("pgadmin depends on db" in m for m in messages)
    assert any(f.code == "BUILD-NO-CMD" for f in findings)
    assert {"IMAGE-UNPINNED", "VAR-UNUSED"} <= codes(findings, WARNING)


def test_required_variable_missing(stack):
    stack.edit("example.env", "POSTGRES_DB=app\n", "")
    findings = check(stack.root)
    assert [str(f) for f in errors(findings)] == [
        "error: [VAR-REQUIRED] required variable POSTGRES_DB is not set in example.env"
    ]


def test_variable_without_default(stack):
    stack.edit("docker-compose.local.yml", "${APP_PORT:-8000}", "${NEW_PORT}")
    assert "VAR-UNSET" in codes(check(stack.root), ERROR)


def test_unused_variable_warning(stack):
    (stack.root / "example.env").write_text(
        (stack.root / "example.env").read_text() + "LEFTOVER=1\n"
    )
    findings = check(stack.root)
    assert [f.message for f in findings] == ["LEFTOVER is set in example.env but never used"]


def test_dot_env_compared_against_example(stack):
    env = (stack.root / "example.env").read_text().replace("TZ=UTC\n", "")
    (stack.root / ".env").write_text(env)
    findings = check(stack.root)  # .env is picked up by default, like compose does
    assert errors(findings) == []
    assert "ENV-MISSING" in codes(findings) and "ENV-PLACEHOLDER" in codes(findings)


def test_db_without_backend_network_is_unreachable(stack):
    data = stack.compose()
    del data["services"]["db"]["networks"]
    stack.write_compose(data)
    found = [f.message for f in errors(check(stack.root)) if f.code == "NET-UNREACHABLE"]
    assert "service app depends on db but they share no network" in found
    assert "service pgadmin depends on db but they share no network" in found
    assert "service app points DATABASE_HOST at db but they share no network" in found


def test_url_hostnames_must_be_reachable(stack):
    data = stack.compose()
    data["services"]["redis"]["networks"] = ["frontend"]
    stack.write_compose(data)
    messages = {f.message for f in errors(check(stack.root))}
    assert "service app points CELERY_BROKER_URL at redis but they share no network" in messages


def test_undeclared_network_and_volume(stack):
    data = stack.compose()
    data["services"]["mailpit"]["networks"] = ["nowhere"]
    data["services"]["mailpit"]["volumes"] = ["ghost:/data"]
    stack.write_compose(data)
    assert {"NET-UNDECLARED", "VOL-UNDECLARED"} <= codes(check(stack.root), ERROR)


def test_core_service_depending_on_profiled_service(stack):
    data = stack.compose()
    data["services"]["app"]["depends_on"]["redis"] = {"condition": "service_started"}
    stack.write_compose(data)
    finding = next(f for f in check(stack.root) if f.code == "DEP-PROFILE")
    assert finding.message == "core service app depends on redis, which only runs with profile(s) worker"


def test_unknown_dependency(stack):
    data = stack.compose()
    data["services"]["app"]["depends_on"]["cache"] = {"condition": "service_started"}
    stack.write_compose(data)
    assert "DEP-UNKNOWN" in codes(check(stack.root), ERROR)


def test_service_healthy_needs_a_healthcheck(stack):
    data = stack.compose()
    data["services"]["db"]["healthcheck"] = {"disable": True}
    stack.write_compose(data)
    findings = check(stack.root)
    assert any(
        f.code == "DEP-NO-HEALTHCHECK" and "app waits for db" in f.message for f in errors(findings)
    )


def test_service_healthy_on_image_without_compose_healthcheck(stack):
    # an image may or may not define HEALTHCHECK itself; we cannot inspect it offline
    data = stack.compose()
    del data["services"]["db"]["healthcheck"]
    stack.write_compose(data)
    findings = check(stack.root)
    assert errors(findings) == []
    assert any(
        f.code == "DEP-HEALTH-UNKNOWN" and "image postgres:16" in f.message for f in findings
    )


def test_dockerfile_healthcheck_counts(stack):
    # nginx waits for app to be healthy; app's healthcheck lives in the Dockerfile
    stack.edit("Dockerfile", "HEALTHCHECK --interval", "# HEALTHCHECK --interval")
    stack.edit("Dockerfile", "    CMD curl -fsS", "#    CMD curl -fsS")
    findings = check(stack.root)
    assert any(f.code == "DEP-NO-HEALTHCHECK" and "nginx waits for app" in f.message for f in findings)


def test_port_conflict(stack):
    stack.edit("example.env", "PGADMIN_PORT=5050", "PGADMIN_PORT=8000")
    finding = next(f for f in check(stack.root) if f.code == "PORT-CONFLICT")
    assert finding.message == "host port 8000/tcp is published by both app and pgadmin"


def test_different_host_ips_do_not_conflict(stack):
    data = stack.compose()
    data["services"]["pgadmin"]["ports"] = ["127.0.0.2:8000:80"]
    stack.write_compose(data)
    assert "PORT-CONFLICT" not in codes(check(stack.root))
    data["services"]["pgadmin"]["ports"] = ["8000:80"]  # all interfaces
    stack.write_compose(data)
    assert "PORT-CONFLICT" in codes(check(stack.root))


def test_build_problems(stack):
    stack.edit("Dockerfile", 'CMD ["python", "main.py"]', "")
    (stack.root / "app" / "requirements.txt").unlink()
    found = codes(check(stack.root), ERROR)
    assert {"BUILD-NO-CMD", "BUILD-COPY-MISSING"} <= found


def test_command_in_compose_satisfies_missing_cmd(stack):
    stack.edit("Dockerfile", 'CMD ["python", "main.py"]', "")
    data = stack.compose()
    data["services"]["app"]["command"] = ["python", "main.py"]
    stack.write_compose(data)
    assert "BUILD-NO-CMD" not in codes(check(stack.root))


def test_app_dir_build_arg_is_followed(stack):
    stack.edit("example.env", "APP_DIR=app", "APP_DIR=missing")
    findings = check(stack.root)
    assert any(f.code == "BIND-MISSING" and "./missing" in f.message for f in errors(findings))
    assert any(f.code == "BUILD-COPY-MISSING" and "missing/requirements.txt" in f.message for f in findings)


def test_missing_optional_pieces_are_only_warnings(stack):
    import shutil

    shutil.rmtree(stack.root / "frontend")
    findings = check(stack.root)
    assert errors(findings) == []
    assert {"BIND-MISSING", "BUILD-COPY-MISSING"} <= codes(findings, WARNING)


def test_missing_dockerfile_and_context(stack):
    (stack.root / "Dockerfile_frontend").unlink()
    data = stack.compose()
    data["services"]["app"]["build"] = {"context": "./nope"}
    stack.write_compose(data)
    found = codes(check(stack.root))
    assert "BUILD-DOCKERFILE" in found and "BUILD-CONTEXT" in found


def test_unpinned_images(stack):
    stack.edit("docker-compose.local.yml", "image: redis:7-alpine", "image: redis")
    stack.edit("Dockerfile", "FROM python:${PYTHON_VERSION}-slim", "FROM python")
    messages = [f.message for f in check(stack.root) if f.code == "IMAGE-UNPINNED"]
    assert "service redis uses unpinned image redis" in messages
    assert "Dockerfile: unpinned base image python" in messages


# --- nginx -----------------------------------------------------------------------------


def test_nginx_syntax_error(stack):
    stack.edit("nginx.conf", "expires 1h;\n    }", "expires 1h;\n")
    finding = next(f for f in check(stack.root) if f.code == "NGINX-SYNTAX")
    assert finding.message.startswith("./nginx.conf: line ")


def test_nginx_template_variable_must_be_provided(stack):
    stack.edit("nginx.conf", "listen 80;", "listen 80;\n    root ${NGINX_ROOT};")
    finding = next(f for f in check(stack.root) if f.code == "NGINX-TEMPLATE-VAR")
    assert "NGINX_ROOT" in finding.message


def test_nginx_placeholders_without_template_mount(stack):
    stack.edit(
        "docker-compose.local.yml",
        "/etc/nginx/templates/default.conf.template",
        "/etc/nginx/conf.d/default.conf",
    )
    assert "NGINX-NOT-TEMPLATE" in codes(check(stack.root), WARNING)


def test_nginx_unknown_and_optional_upstreams(stack):
    stack.edit("nginx.conf", "server app:8000;", "server api:8000;")
    assert "NGINX-UPSTREAM" in codes(check(stack.root), ERROR)
    stack.edit("nginx.conf", "server api:8000;", "server frontend:8080;")
    findings = check(stack.root)
    assert "NGINX-OPTIONAL-UPSTREAM" in codes(findings)
    # frontend is only on the frontend network, nginx is on both -> reachable
    assert "NET-UNREACHABLE" not in codes(findings)


def test_nginx_wrong_upstream_port(stack):
    stack.edit("nginx.conf", "server app:8000;", "server app:9999;")
    finding = next(f for f in check(stack.root) if f.code == "NGINX-PORT")
    assert finding.message.endswith("but app exposes 8000")


def test_nginx_direct_proxy_pass_host_checked(stack):
    stack.edit("nginx.conf", "proxy_pass http://app_upstream;", "proxy_pass http://vault:8200;")
    findings = check(stack.root)
    assert "NGINX-OPTIONAL-UPSTREAM" in codes(findings)


def test_nginx_must_listen_on_published_port(stack):
    stack.edit("nginx.conf", "listen 80;", "listen 8080;")
    assert "NGINX-LISTEN" in codes(check(stack.root), WARNING)


def test_nginx_unreachable_upstream(stack):
    data = stack.compose()
    data["services"]["nginx"]["networks"] = ["frontend"]
    stack.write_compose(data)
    messages = [f.message for f in errors(check(stack.root))]
    assert "./nginx.conf: nginx proxies to app but they share no network" in messages


# --- init scripts -----------------------------------------------------------------------


def test_init_script_env_must_be_passed(stack):
    data = stack.compose()
    del data["services"]["db"]["environment"]["DATABASE_PASSWORD"]
    stack.write_compose(data)
    finding = next(f for f in check(stack.root) if f.code == "INITDB-ENV")
    assert finding.message == (
        "./init-script.sh reads $DATABASE_PASSWORD but service db does not set it"
    )


def test_init_script_not_executable(stack):
    os.chmod(stack.root / "init-script.sh", 0o644)
    if os.access(stack.root / "init-script.sh", os.X_OK):
        pytest.skip("running as a user that can execute any file")
    assert "INITDB-NOT-EXECUTABLE" in codes(check(stack.root), WARNING)


def test_interpolation_and_structure_errors(stack):
    stack.compose_path.write_text("services:\n  a:\n    image: ${BROKEN\n")
    assert "VAR-SYNTAX" in codes(check(stack.root))
    stack.compose_path.write_text("services:\n  a:\n    image: x:1\n    ports: ['nope']\n")
    assert codes(check(stack.root), ERROR) >= {"COMPOSE-STRUCTURE"}
