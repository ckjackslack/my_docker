"""Placeholder application for the `app` service.

Replace this directory (or point APP_DIR elsewhere) with your real project. It exists so the
stack runs end to end out of the box: it listens on :8000, answers ``/health`` (used by the
image HEALTHCHECK and by nginx's ``depends_on``) and reports whether Postgres is reachable.
Standard library only.
"""

from __future__ import annotations

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Mapping

Probe = Callable[[str, int], bool]


def tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def status(env: Mapping[str, str], probe: Probe) -> tuple[int, dict]:
    """Build the health payload from the environment; 503 if the database is unreachable."""
    host = env.get("DATABASE_HOST", "db")
    try:
        port = int(env.get("DATABASE_PORT", "5432"))
    except ValueError:
        return 500, {"status": "error", "detail": "DATABASE_PORT is not an integer"}
    reachable = probe(host, port)
    payload = {
        "status": "ok" if reachable else "degraded",
        "database": {
            "host": host,
            "port": port,
            "name": env.get("DATABASE_NAME", ""),
            "reachable": reachable,
        },
    }
    return (200 if reachable else 503), payload


def make_handler(env: Mapping[str, str], probe: Probe) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if self.path in ("/", "/health"):
                code, payload = status(env, probe)
            else:
                code, payload = 404, {"status": "not found", "path": self.path}
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            if self.path != "/health":  # keep healthcheck noise out of the logs
                super().log_message(fmt, *args)

    return Handler


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(os.environ, tcp_reachable))
    print(f"placeholder app listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
