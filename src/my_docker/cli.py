"""``my-docker`` command line: generate ``.env``, check the stack, list services."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from my_docker import __version__
from my_docker.checks import ERROR, resolved_compose, run_checks
from my_docker.compose import ComposeError
from my_docker.envfile import EnvFileError, parse_assignment, render_env
from my_docker.interpolation import InterpolationError
from my_docker.project import DEFAULT_COMPOSE, DEFAULT_EXAMPLE, ProjectError, load_project

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2


class CliError(Exception):
    pass


def _common(parser: argparse.ArgumentParser, with_env: bool = True) -> None:
    parser.add_argument(
        "-C", "--directory", default=".", metavar="DIR",
        help="stack directory (default: current directory)",
    )
    if with_env:
        parser.add_argument(
            "-f", "--file", default=DEFAULT_COMPOSE, metavar="FILE",
            help=f"compose file inside DIR (default: {DEFAULT_COMPOSE})",
        )
        parser.add_argument(
            "--env-file", metavar="FILE",
            help="env file to resolve ${VAR}s with (default: DIR/.env, else DIR/example.env)",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="my-docker",
        description="Helper for the my_docker local development stack.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    env = sub.add_parser(
        "env", help="create .env from example.env with random secrets",
        description="Render .env from example.env. Every value 'change-me' becomes a fresh "
        "random secret; --set overrides individual values.",
    )
    _common(env, with_env=False)
    env.add_argument("--example", default=DEFAULT_EXAMPLE, metavar="FILE",
                     help=f"template inside DIR (default: {DEFAULT_EXAMPLE})")
    env.add_argument("-o", "--output", default=".env", metavar="FILE",
                     help="output file inside DIR (default: .env), '-' for stdout")
    env.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="override a value (repeatable)")
    env.add_argument("--force", action="store_true", help="overwrite an existing output file")

    check = sub.add_parser(
        "check", help="statically validate the compose stack (no Docker daemon needed)",
        description="Check variables, networks, volumes, dependencies, ports, Dockerfiles, "
        "nginx config and init scripts. Exit status 1 if errors are found.",
    )
    _common(check)
    check.add_argument("--strict", action="store_true", help="treat warnings as errors")
    check.add_argument("-q", "--quiet", action="store_true", help="only print errors")

    services = sub.add_parser("services", help="list services, profiles and published ports")
    _common(services)
    services.add_argument("--profile", action="append", default=[], metavar="NAME",
                          help="only services started with these profiles ('*' = all)")
    return parser


def cmd_env(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    example = root / args.example
    try:
        example_text = example.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read {example}: {exc.strerror}") from exc
    try:
        overrides = dict(parse_assignment(item) for item in args.set)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    try:
        result = render_env(example_text, overrides)
    except EnvFileError as exc:
        raise CliError(f"{example}: {exc}") from exc

    if args.output == "-":
        sys.stdout.write(result.text)
        return EXIT_OK
    output = root / args.output
    if output.exists() and not args.force:
        raise CliError(f"{output} already exists (use --force to overwrite)")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(result.text)
    os.chmod(output, 0o600)
    print(f"wrote {output}")
    if result.generated:
        print(f"generated secrets for: {', '.join(result.generated)}")
    if result.overridden:
        print(f"overridden: {', '.join(result.overridden)}")
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    project = load_project(args.directory, args.file, args.env_file)
    findings = run_checks(project)
    env_label = project.env_path or "(no env file)"
    shown = [f for f in findings if not (args.quiet and f.level != ERROR)]
    for finding in shown:
        print(finding)
    n_err = sum(f.level == ERROR for f in findings)
    n_warn = len(findings) - n_err
    print(f"{project.compose_path} with {env_label}: {n_err} error(s), {n_warn} warning(s)")
    if n_err or (args.strict and n_warn):
        return EXIT_FINDINGS
    return EXIT_OK


def cmd_services(args: argparse.Namespace) -> int:
    project = load_project(args.directory, args.file, args.env_file)
    try:
        compose = resolved_compose(project)
    except (InterpolationError, ComposeError) as exc:
        raise CliError(f"{exc} (run `my-docker check` for details)") from exc
    selected = compose.active(args.profile) if args.profile else list(compose.services.values())
    rows = [("SERVICE", "PROFILES", "PORTS", "DEPENDS ON")]
    for svc in sorted(selected, key=lambda s: (not s.is_core, s.profiles, s.name)):
        ports = ", ".join(
            f"{p.host_ip + ':' if p.host_ip else ''}{p.published}->{p.target}"
            if p.published else p.target
            for p in svc.ports
        )
        rows.append((svc.name, ",".join(svc.profiles) or "(core)", ports or "-",
                     ", ".join(svc.depends_on) or "-"))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)) + "  " + row[3])
    print(f"profiles: {', '.join(compose.profiles()) or '(none)'}")
    return EXIT_OK


COMMANDS = {"env": cmd_env, "check": cmd_check, "services": cmd_services}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (CliError, ProjectError) as exc:
        print(f"my-docker: error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
