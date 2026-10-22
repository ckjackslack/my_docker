"""The placeholder app shipped in app/ (stdlib only)."""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import REPO

spec = importlib.util.spec_from_file_location("placeholder_app", REPO / "app" / "main.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def test_status_ok_and_degraded():
    calls = []

    def probe(host, port):
        calls.append((host, port))
        return True

    code, payload = app.status({"DATABASE_HOST": "db", "DATABASE_NAME": "app"}, probe)
    assert code == 200 and payload["status"] == "ok"
    assert payload["database"] == {"host": "db", "port": 5432, "name": "app", "reachable": True}
    assert calls == [("db", 5432)]

    code, payload = app.status({"DATABASE_PORT": "6543"}, lambda h, p: False)
    assert code == 503 and payload["status"] == "degraded"
    assert payload["database"]["port"] == 6543


def test_status_bad_port():
    code, payload = app.status({"DATABASE_PORT": "x"}, lambda h, p: True)
    assert code == 500 and "DATABASE_PORT" in payload["detail"]


def test_tcp_reachable_against_local_listener():
    import socket

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        assert app.tcp_reachable("127.0.0.1", port)
    assert not app.tcp_reachable("127.0.0.1", port, timeout=0.2)


@pytest.fixture
def server():
    state = {"up": True}
    httpd = ThreadingHTTPServer(
        ("127.0.0.1", 0), app.make_handler({"DATABASE_HOST": "db"}, lambda h, p: state["up"])
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    httpd.shutdown()
    httpd.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_endpoints(server):
    base, state = server
    assert get(base + "/health")[0] == 200
    code, payload = get(base + "/")
    assert code == 200 and payload["database"]["host"] == "db"
    state["up"] = False
    assert get(base + "/health")[0] == 503
    assert get(base + "/missing") == (404, {"status": "not found", "path": "/missing"})
