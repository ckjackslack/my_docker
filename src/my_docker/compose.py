"""A small, normalised model of a (already interpolated) compose file.

Only what the static checks need: services with their image/build, profiles, networks,
dependencies, ports, mounts, environment and healthcheck. Short and long syntax are both
accepted and normalised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PORT_NUMBER = re.compile(r"\d+(-\d+)?\Z")


class ComposeError(ValueError):
    """The compose document does not have the expected structure."""


@dataclass(frozen=True)
class Port:
    target: str
    published: str | None = None
    host_ip: str | None = None
    protocol: str = "tcp"


@dataclass(frozen=True)
class Mount:
    kind: str  # "bind" | "volume" | "anonymous"
    target: str
    source: str | None = None
    read_only: bool = False


@dataclass(frozen=True)
class Build:
    context: str = "."
    dockerfile: str = "Dockerfile"
    args: dict[str, str] = field(default_factory=dict)


@dataclass
class Service:
    name: str
    image: str | None = None
    build: Build | None = None
    profiles: tuple[str, ...] = ()
    networks: tuple[str, ...] = ("default",)
    depends_on: dict[str, str] = field(default_factory=dict)
    ports: list[Port] = field(default_factory=list)
    mounts: list[Mount] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    command: Any = None
    healthcheck: dict | None = None

    @property
    def is_core(self) -> bool:
        """Started by a plain ``docker compose up`` (no profile needed)."""
        return not self.profiles

    @property
    def healthcheck_disabled(self) -> bool:
        hc = self.healthcheck or {}
        return bool(hc.get("disable")) or hc.get("test") in (["NONE"], "NONE")


@dataclass
class ComposeFile:
    services: dict[str, Service]
    networks: set[str]
    volumes: set[str]

    def profiles(self) -> list[str]:
        return sorted({p for s in self.services.values() for p in s.profiles})

    def active(self, profiles: list[str] | tuple[str, ...] = ()) -> list[Service]:
        """Services ``docker compose --profile ... up`` would start."""
        wanted = set(profiles)
        return [
            s
            for s in self.services.values()
            if s.is_core or "*" in wanted or wanted.intersection(s.profiles)
        ]


def parse_port(spec: Any) -> Port:
    if isinstance(spec, dict):
        target = str(spec.get("target", ""))
        published = spec.get("published")
        port = Port(
            target=target,
            published=None if published is None else str(published),
            host_ip=spec.get("host_ip"),
            protocol=str(spec.get("protocol", "tcp")),
        )
    else:
        text = str(spec)
        text, _, protocol = text.partition("/")
        parts = text.rsplit(":", 2)
        if len(parts) == 3:
            host_ip, published, target = parts
        elif len(parts) == 2:
            host_ip, (published, target) = None, parts
        else:
            host_ip, published, target = None, None, parts[0]
        port = Port(
            target=target,
            published=published or None,
            host_ip=(host_ip or "").strip("[]") or None,
            protocol=protocol or "tcp",
        )
    if not _PORT_NUMBER.match(port.target):
        raise ComposeError(f"invalid container port in {spec!r}")
    if port.published is not None and not _PORT_NUMBER.match(port.published):
        raise ComposeError(f"invalid published port in {spec!r}")
    return port


def parse_mount(spec: Any) -> Mount:
    if isinstance(spec, dict):
        kind = spec.get("type", "volume")
        source = spec.get("source")
        if kind == "volume" and not source:
            kind = "anonymous"
        return Mount(kind, str(spec.get("target", "")), source, bool(spec.get("read_only")))
    parts = str(spec).split(":")
    if len(parts) == 1:
        return Mount("anonymous", parts[0])
    source, target = parts[0], parts[1]
    mode = parts[2] if len(parts) > 2 else ""
    kind = "bind" if source.startswith((".", "/", "~")) else "volume"
    return Mount(kind, target, source, "ro" in mode.split(","))


def _environment(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): "" if v is None else str(v) for k, v in value.items()}
    if isinstance(value, list):
        env = {}
        for item in value:
            key, _, val = str(item).partition("=")
            env[key] = val
        return env
    raise ComposeError("environment must be a mapping or a list")


def _names(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [str(k) for k in value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ComposeError(f"{what} must be a mapping or a list")


def _service(name: str, raw: Any) -> Service:
    if not isinstance(raw, dict):
        raise ComposeError(f"service {name!r} must be a mapping")
    build = raw.get("build")
    if isinstance(build, str):
        build = Build(context=build)
    elif isinstance(build, dict):
        build = Build(
            context=str(build.get("context", ".")),
            dockerfile=str(build.get("dockerfile", "Dockerfile")),
            args=_environment(build.get("args")),
        )
    depends = raw.get("depends_on") or {}
    if isinstance(depends, list):
        depends_on = {str(d): "service_started" for d in depends}
    elif isinstance(depends, dict):
        depends_on = {
            str(d): str((cfg or {}).get("condition", "service_started"))
            for d, cfg in depends.items()
        }
    else:
        raise ComposeError(f"service {name!r}: depends_on must be a mapping or a list")
    networks = _names(raw.get("networks"), f"service {name!r}: networks") or ["default"]
    return Service(
        name=name,
        image=raw.get("image"),
        build=build,
        profiles=tuple(str(p) for p in raw.get("profiles") or ()),
        networks=tuple(networks),
        depends_on=depends_on,
        ports=[parse_port(p) for p in raw.get("ports") or ()],
        mounts=[parse_mount(v) for v in raw.get("volumes") or ()],
        environment=_environment(raw.get("environment")),
        command=raw.get("command"),
        healthcheck=raw.get("healthcheck"),
    )


def parse_compose(data: Any) -> ComposeFile:
    if not isinstance(data, dict):
        raise ComposeError("compose file must be a mapping at the top level")
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise ComposeError("compose file defines no services")
    return ComposeFile(
        services={str(n): _service(str(n), raw) for n, raw in services.items()},
        networks=set(_names(data.get("networks"), "networks")) | {"default"},
        volumes=set(_names(data.get("volumes"), "volumes")),
    )
