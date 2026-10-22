"""Compose model, Dockerfile and nginx parsers."""

import pytest

from my_docker import dockerfile, nginx
from my_docker.compose import ComposeError, Mount, Port, parse_compose, parse_mount, parse_port

# --- compose -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("8000", Port("8000")),
        ("8000:80", Port("80", "8000")),
        ("127.0.0.1:8000:80", Port("80", "8000", "127.0.0.1")),
        ("[::1]:53:53/udp", Port("53", "53", "::1", "udp")),
        (5432, Port("5432")),
        ({"target": 80, "published": "8080", "host_ip": "127.0.0.1"}, Port("80", "8080", "127.0.0.1")),
        ("9000-9001:9000-9001", Port("9000-9001", "9000-9001")),
    ],
)
def test_parse_port(spec, expected):
    assert parse_port(spec) == expected


@pytest.mark.parametrize("spec", ["abc", "127.0.0.1::x", "x:80"])
def test_parse_port_invalid(spec):
    with pytest.raises(ComposeError):
        parse_port(spec)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("/app/node_modules", Mount("anonymous", "/app/node_modules")),
        ("data:/var/lib/x", Mount("volume", "/var/lib/x", "data")),
        ("./conf:/etc/x:ro", Mount("bind", "/etc/x", "./conf", True)),
        ("/abs:/y:rw,z", Mount("bind", "/y", "/abs", False)),
        ({"type": "bind", "source": "./a", "target": "/a", "read_only": True}, Mount("bind", "/a", "./a", True)),
        ({"type": "volume", "target": "/t"}, Mount("anonymous", "/t", None)),
    ],
)
def test_parse_mount(spec, expected):
    assert parse_mount(spec) == expected


def test_parse_compose_normalises_short_and_long_syntax():
    compose = parse_compose(
        {
            "services": {
                "web": {
                    "build": ".",
                    "depends_on": ["db"],
                    "environment": ["A=1", "B"],
                    "profiles": ["p"],
                },
                "db": {
                    "image": "postgres:16",
                    "networks": {"back": {}},
                    "depends_on": {"x": {"condition": "service_healthy"}},
                    "healthcheck": {"disable": True},
                },
            },
            "networks": {"back": None},
            "volumes": {"data": None},
        }
    )
    web, db = compose.services["web"], compose.services["db"]
    assert web.build.context == "." and web.build.dockerfile == "Dockerfile"
    assert web.depends_on == {"db": "service_started"}
    assert web.environment == {"A": "1", "B": ""}
    assert web.networks == ("default",) and not web.is_core
    assert db.networks == ("back",) and db.depends_on == {"x": "service_healthy"}
    assert db.healthcheck_disabled
    assert compose.networks == {"back", "default"} and compose.volumes == {"data"}
    assert [s.name for s in compose.active()] == ["db"]
    assert [s.name for s in compose.active(["p"])] == ["web", "db"]
    assert compose.profiles() == ["p"]


@pytest.mark.parametrize(
    "data",
    [[], {"services": {}}, {"services": {"a": "nope"}}, {"services": {"a": {"environment": 3}}}],
)
def test_parse_compose_structure_errors(data):
    with pytest.raises(ComposeError):
        parse_compose(data)


# --- Dockerfile ----------------------------------------------------------------------

DOCKERFILE = """\
# syntax comment
ARG BASE=3.12
FROM python:${BASE}-slim AS build
ARG APP_DIR=app
RUN apt-get update \\
    # comment inside continuation
    && echo hi
COPY ${APP_DIR}/requirements.txt ./
COPY --chown=u:u ${APP_DIR}/ ./
COPY --from=build /x /y
COPY ["json", "form"]
EXPOSE 8000/tcp 9000
HEALTHCHECK CMD curl -f http://localhost/
CMD ["python", "main.py"]
"""


def test_dockerfile_parsing():
    ins = dockerfile.parse_dockerfile(DOCKERFILE)
    assert [i.keyword for i in ins][:4] == ["ARG", "FROM", "ARG", "RUN"]
    run = ins[3]
    assert run.args == "apt-get update && echo hi" and run.lineno == 5
    values = dockerfile.arg_values(ins, {"APP_DIR": "src"})
    assert values == {"BASE": "3.12", "APP_DIR": "src"}
    assert dockerfile.base_images(ins, values) == ["python:3.12-slim"]
    assert dockerfile.copy_sources(ins, values) == ["src/requirements.txt", "src/"]
    assert dockerfile.exposed_ports(ins, values) == {"8000", "9000"}
    assert dockerfile.has_instruction(ins, "CMD")
    assert not dockerfile.has_instruction(ins, "ENTRYPOINT")


@pytest.mark.parametrize(
    ("image", "unpinned"),
    [
        ("postgres", True),
        ("postgres:latest", True),
        ("dpage/pgadmin4", True),
        ("postgres:16", False),
        ("localhost:5000/img", True),
        ("localhost:5000/img:1.0", False),
        ("img@sha256:abc", False),
    ],
)
def test_is_unpinned(image, unpinned):
    assert dockerfile.is_unpinned(image) is unpinned


# --- nginx ---------------------------------------------------------------------------

NGINX = """\
upstream backend { server app:8000; server other; }
server {
    listen 80;
    listen [::]:8443 ssl;
    server_name ${NGINX_SERVER_NAME} "quoted name"; # comment ${IGNORED}
    location / { proxy_pass http://backend; }
    location /api/ { proxy_pass http://api:9000/v1/; proxy_set_header Host $host; }
}
"""


def test_nginx_parse_and_queries():
    tree = nginx.parse(NGINX)
    assert [d.name for d in tree] == ["upstream", "server"]
    server_name = next(nginx.iter_directives(tree, "server_name"))
    assert server_name.args == ["${NGINX_SERVER_NAME}", "quoted name"]
    assert server_name.lineno == 5
    assert nginx.upstreams(tree) == {"backend": ["app:8000", "other"]}
    assert nginx.proxy_targets(tree) == ["backend", "api:9000"]
    assert nginx.listen_ports(tree) == {"80", "8443"}
    assert nginx.template_vars(NGINX) == {"NGINX_SERVER_NAME"}
    assert nginx.split_host_port("app:8000") == ("app", "8000")
    assert nginx.split_host_port("app") == ("app", "80")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("server { listen 80 }", "not terminated by ';'"),
        ("server { listen 80;", "unclosed"),
        ("listen 80; }", "unexpected '}'"),
        ("{ listen 80; }", "without a directive name"),
        ("listen 80", "not terminated"),
        ("server_name 'oops;", "unterminated quoted string"),
        (";", "unexpected ';'"),
    ],
)
def test_nginx_syntax_errors(text, message):
    with pytest.raises(nginx.NginxSyntaxError, match=message):
        nginx.parse(text)
