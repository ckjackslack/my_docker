"""Static checks for the compose stack: the things ``docker compose up`` would trip over.

Every check returns :class:`Finding` objects. Errors are problems that break the stack (or a
profile of it); warnings are smells. Checks never talk to a Docker daemon.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from urllib.parse import urlsplit

from my_docker import dockerfile, nginx
from my_docker.compose import ComposeError, ComposeFile, Service, parse_compose
from my_docker.envfile import PLACEHOLDER
from my_docker.interpolation import (
    InterpolationError,
    find_refs,
    interpolate_tree,
    walk_strings,
)
from my_docker.project import Project

ERROR = "error"
WARNING = "warning"

_SHELL_VAR = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)")
_SHELL_ASSIGN = re.compile(r"^\s*(?:local\s+|export\s+)?([A-Z][A-Z0-9_]*)=", re.M)
_SHELL_IGNORED = {"PATH", "HOME", "PWD", "IFS", "SHELL", "USER", "LANG", "TERM"}
_WILDCARD = re.compile(r"[*?\[]")


@dataclass(frozen=True, order=True)
class Finding:
    level: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: [{self.code}] {self.message}"


def errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == ERROR]


class _Checker:
    def __init__(self, project: Project):
        self.p = project
        self.findings: list[Finding] = []
        self._dockerfiles: dict[str, list[dockerfile.Instruction] | None] = {}

    def add(self, level: str, code: str, message: str) -> None:
        self.findings.append(Finding(level, code, message))

    # --- variables ------------------------------------------------------------------
    def check_variables(self) -> None:
        refs = defaultdict(list)
        for text in walk_strings(self.p.compose_data):
            try:
                for ref in find_refs(text):
                    refs[ref.name].append(ref)
            except InterpolationError as exc:
                self.add(ERROR, "VAR-SYNTAX", str(exc))
        env_name = self.p.env_path.name if self.p.env_path else "the environment"
        for name, group in sorted(refs.items()):
            if name in self.p.env:
                continue
            if any(r.required for r in group):
                self.add(ERROR, "VAR-REQUIRED", f"required variable {name} is not set in {env_name}")
            elif not all(r.has_fallback for r in group):
                self.add(
                    ERROR,
                    "VAR-UNSET",
                    f"{name} has no default and is not set in {env_name} "
                    "(compose would substitute an empty string)",
                )
        for name in sorted(set(self.p.env) - set(refs)):
            self.add(WARNING, "VAR-UNUSED", f"{name} is set in {env_name} but never used")

        if self.p.example_env is not None:
            for name in sorted(set(self.p.example_env) - set(self.p.env)):
                self.add(WARNING, "ENV-MISSING", f"{name} from example.env is missing in {env_name}")
            for name, value in sorted(self.p.env.items()):
                if value.strip().lower() == PLACEHOLDER:
                    self.add(
                        WARNING,
                        "ENV-PLACEHOLDER",
                        f"{name} in {env_name} is still '{PLACEHOLDER}' (run `my-docker env`)",
                    )

    # --- helpers ----------------------------------------------------------------------
    def instructions(self, svc: Service) -> list[dockerfile.Instruction] | None:
        assert svc.build is not None
        path = str(self.p.path(svc.build.context) / svc.build.dockerfile)
        if path not in self._dockerfiles:
            text = self.p.read_text(path)
            self._dockerfiles[path] = None if text is None else dockerfile.parse_dockerfile(text)
        return self._dockerfiles[path]

    def has_healthcheck(self, svc: Service) -> bool | None:
        """True/False when known; None when it depends on an image we cannot inspect."""
        if svc.healthcheck_disabled:
            return False
        if svc.healthcheck and svc.healthcheck.get("test"):
            return True
        if svc.build is not None:
            instructions = self.instructions(svc) or []
            return any(
                i.keyword == "HEALTHCHECK" and i.args.split()[:1] != ["NONE"]
                for i in instructions
            )
        return None

    @staticmethod
    def shares_network(a: Service, b: Service) -> bool:
        return bool(set(a.networks) & set(b.networks))

    def level_for(self, svc: Service) -> str:
        """Missing optional pieces only matter once their profile is enabled."""
        return ERROR if svc.is_core else WARNING

    # --- per service ----------------------------------------------------------------
    def check_services(self, compose: ComposeFile) -> None:
        services = compose.services
        for svc in services.values():
            if not svc.image and svc.build is None:
                self.add(ERROR, "SVC-NO-IMAGE", f"service {svc.name} has neither image nor build")
            if svc.image and dockerfile.is_unpinned(svc.image):
                self.add(WARNING, "IMAGE-UNPINNED", f"service {svc.name} uses unpinned image {svc.image}")

            for net in svc.networks:
                if net not in compose.networks:
                    self.add(ERROR, "NET-UNDECLARED", f"service {svc.name} uses undeclared network {net}")

            for mount in svc.mounts:
                if mount.kind == "volume" and mount.source not in compose.volumes:
                    self.add(
                        ERROR, "VOL-UNDECLARED",
                        f"service {svc.name} uses undeclared volume {mount.source}",
                    )
                elif mount.kind == "bind" and not self.p.exists(mount.source or ""):
                    self.add(
                        self.level_for(svc), "BIND-MISSING",
                        f"service {svc.name} bind-mounts missing path {mount.source}",
                    )

            for dep, condition in svc.depends_on.items():
                target = services.get(dep)
                if target is None:
                    self.add(ERROR, "DEP-UNKNOWN", f"service {svc.name} depends on unknown service {dep}")
                    continue
                if svc.is_core and not target.is_core:
                    self.add(
                        ERROR, "DEP-PROFILE",
                        f"core service {svc.name} depends on {dep}, which only runs with "
                        f"profile(s) {', '.join(target.profiles)}",
                    )
                if not self.shares_network(svc, target):
                    self.add(
                        ERROR, "NET-UNREACHABLE",
                        f"service {svc.name} depends on {dep} but they share no network",
                    )
                if condition == "service_healthy":
                    health = self.has_healthcheck(target)
                    if health is False:
                        self.add(
                            ERROR, "DEP-NO-HEALTHCHECK",
                            f"{svc.name} waits for {dep} to be healthy but {dep} has no healthcheck",
                        )
                    elif health is None:
                        self.add(
                            WARNING, "DEP-HEALTH-UNKNOWN",
                            f"{svc.name} waits for {dep} to be healthy; cannot verify that "
                            f"image {target.image} defines a healthcheck",
                        )

            for key, value in svc.environment.items():
                host = value if value in services else None
                if host is None and "://" in value:
                    try:
                        host = urlsplit(value).hostname
                    except ValueError:
                        host = None
                if host in services and host != svc.name:
                    if not self.shares_network(svc, services[host]):
                        self.add(
                            ERROR, "NET-UNREACHABLE",
                            f"service {svc.name} points {key} at {host} but they share no network",
                        )

            if svc.build is not None:
                self.check_build(svc)

    def check_build(self, svc: Service) -> None:
        assert svc.build is not None
        level = self.level_for(svc)
        context = svc.build.context
        if not self.p.is_dir(context):
            self.add(level, "BUILD-CONTEXT", f"service {svc.name}: build context {context} not found")
            return
        instructions = self.instructions(svc)
        if instructions is None:
            self.add(
                ERROR, "BUILD-DOCKERFILE",
                f"service {svc.name}: Dockerfile {svc.build.dockerfile} not found in {context}",
            )
            return
        values = dockerfile.arg_values(instructions, svc.build.args)
        for image in dockerfile.base_images(instructions, values):
            if dockerfile.is_unpinned(image):
                self.add(WARNING, "IMAGE-UNPINNED", f"{svc.build.dockerfile}: unpinned base image {image}")
        if not dockerfile.has_instruction(instructions, "CMD", "ENTRYPOINT") and not svc.command:
            self.add(
                ERROR, "BUILD-NO-CMD",
                f"service {svc.name}: {svc.build.dockerfile} has no CMD/ENTRYPOINT and the "
                "service sets no command, so the container exits immediately",
            )
        ctx = self.p.path(context)
        for source in dockerfile.copy_sources(instructions, values):
            if _WILDCARD.search(source):
                if not any(ctx.glob(source)):
                    self.add(
                        WARNING, "BUILD-COPY-MISSING",
                        f"service {svc.name}: COPY pattern {source} matches nothing in {context}",
                    )
            elif not (ctx / source).exists():
                self.add(
                    level, "BUILD-COPY-MISSING",
                    f"service {svc.name}: COPY source {source} not found in {context}",
                )

    def check_ports(self, compose: ComposeFile) -> None:
        published = [
            (svc, port) for svc in compose.services.values() for port in svc.ports if port.published
        ]
        wildcard = {None, "", "0.0.0.0", "::"}
        for (a, pa), (b, pb) in combinations(published, 2):
            if pa.published != pb.published or pa.protocol != pb.protocol:
                continue
            if pa.host_ip == pb.host_ip or pa.host_ip in wildcard or pb.host_ip in wildcard:
                self.add(
                    ERROR, "PORT-CONFLICT",
                    f"host port {pa.published}/{pa.protocol} is published by both "
                    f"{a.name} and {b.name}",
                )

    # --- nginx ------------------------------------------------------------------------
    def check_nginx(self, compose: ComposeFile) -> None:
        for svc in compose.services.values():
            for mount in svc.mounts:
                if mount.kind != "bind" or not mount.target.startswith("/etc/nginx/"):
                    continue
                if not mount.source or self.p.is_dir(mount.source):
                    continue
                text = self.p.read_text(mount.source)
                if text is None:
                    continue  # reported as BIND-MISSING
                self.check_nginx_conf(compose, svc, mount.source, mount.target, text)

    def check_nginx_conf(
        self, compose: ComposeFile, svc: Service, source: str, target: str, text: str
    ) -> None:
        try:
            tree = nginx.parse(text)
        except nginx.NginxSyntaxError as exc:
            self.add(ERROR, "NGINX-SYNTAX", f"{source}: {exc}")
            return

        placeholders = nginx.template_vars(text)
        if target.startswith("/etc/nginx/templates/") and target.endswith(".template"):
            for name in sorted(placeholders - set(svc.environment)):
                self.add(
                    ERROR, "NGINX-TEMPLATE-VAR",
                    f"{source} uses ${{{name}}} but service {svc.name} does not set {name}",
                )
        elif placeholders:
            self.add(
                WARNING, "NGINX-NOT-TEMPLATE",
                f"{source} contains ${{...}} placeholders but is not mounted under "
                "/etc/nginx/templates/*.template, so they are never substituted",
            )

        upstream_map = nginx.upstreams(tree)
        addresses = [a for servers in upstream_map.values() for a in servers]
        addresses += [t for t in nginx.proxy_targets(tree) if t not in upstream_map]
        for address in addresses:
            if "$" in address:
                continue
            host, port = nginx.split_host_port(address)
            if host.startswith("unix:"):
                continue
            backend = compose.services.get(host)
            if backend is None:
                self.add(
                    ERROR, "NGINX-UPSTREAM",
                    f"{source}: upstream host {host} is not a service in the compose file",
                )
                continue
            if not self.shares_network(svc, backend):
                self.add(
                    ERROR, "NET-UNREACHABLE",
                    f"{source}: {svc.name} proxies to {host} but they share no network",
                )
            if not backend.is_core and not set(backend.profiles) & set(svc.profiles):
                self.add(
                    WARNING, "NGINX-OPTIONAL-UPSTREAM",
                    f"{source}: {host} only runs with profile(s) {', '.join(backend.profiles)}; "
                    "nginx fails to start when it cannot resolve the upstream",
                )
            known = {p.target for p in backend.ports}
            if backend.build is not None:
                instructions = self.instructions(backend) or []
                known |= dockerfile.exposed_ports(
                    instructions, dockerfile.arg_values(instructions, backend.build.args)
                )
            if known and port not in known:
                self.add(
                    WARNING, "NGINX-PORT",
                    f"{source}: proxies to {host}:{port} but {host} exposes {', '.join(sorted(known))}",
                )

        listening = nginx.listen_ports(tree)
        for port in svc.ports:
            if port.target not in listening:
                self.add(
                    WARNING, "NGINX-LISTEN",
                    f"service {svc.name} publishes container port {port.target} but "
                    f"{source} does not listen on it",
                )

    # --- init scripts -----------------------------------------------------------------
    def check_init_scripts(self, compose: ComposeFile) -> None:
        for svc in compose.services.values():
            for mount in svc.mounts:
                if mount.kind != "bind" or not mount.target.startswith("/docker-entrypoint-initdb.d"):
                    continue
                source = mount.source or ""
                if not source.endswith(".sh"):
                    continue
                text = self.p.read_text(source)
                if text is None:
                    continue  # reported as BIND-MISSING
                if not self.p.is_executable(source):
                    self.add(
                        WARNING, "INITDB-NOT-EXECUTABLE",
                        f"{source} is not executable; the postgres entrypoint will source it "
                        "instead of running it",
                    )
                code = "\n".join(line.split("#", 1)[0] for line in text.splitlines()[1:])
                used = set(_SHELL_VAR.findall(code))
                assigned = set(_SHELL_ASSIGN.findall(code))
                for name in sorted(used - assigned - set(svc.environment) - _SHELL_IGNORED):
                    if name.startswith(("PG", "BASH")):
                        continue
                    self.add(
                        WARNING, "INITDB-ENV",
                        f"{source} reads ${name} but service {svc.name} does not set it",
                    )

    # --- driver -----------------------------------------------------------------------
    def run(self) -> list[Finding]:
        self.check_variables()
        try:
            data = interpolate_tree(self.p.compose_data, self.p.env)
        except InterpolationError as exc:
            if not errors(self.findings):
                self.add(ERROR, "VAR-INTERPOLATION", str(exc))
            return self.findings
        try:
            compose = parse_compose(data)
        except ComposeError as exc:
            self.add(ERROR, "COMPOSE-STRUCTURE", str(exc))
            return self.findings
        self.check_services(compose)
        self.check_ports(compose)
        self.check_nginx(compose)
        self.check_init_scripts(compose)
        return self.findings


def run_checks(project: Project) -> list[Finding]:
    """Run every check; findings are deduplicated and sorted errors-first."""
    findings = _Checker(project).run()
    return sorted(set(findings), key=lambda f: (f.level != ERROR, f.code, f.message))


def resolved_compose(project: Project) -> ComposeFile:
    """The compose model with the project's env applied (raises on bad input)."""
    return parse_compose(interpolate_tree(project.compose_data, project.env))
