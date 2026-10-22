"""Loading a stack directory from disk (the only module of the checker that does I/O)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from my_docker.envfile import EnvFileError, env_dict

DEFAULT_COMPOSE = "docker-compose.local.yml"
DEFAULT_EXAMPLE = "example.env"


class ProjectError(Exception):
    """The stack directory cannot be loaded (missing file, invalid YAML/env syntax)."""


@dataclass
class Project:
    root: Path
    compose_path: Path
    compose_data: Any
    env: dict[str, str]
    env_path: Path | None
    example_env: dict[str, str] | None = None
    _cache: dict[str, str | None] = field(default_factory=dict, repr=False)

    # --- file access relative to the project root -------------------------------------
    def path(self, rel: str) -> Path:
        p = Path(os.path.expanduser(rel))
        return p if p.is_absolute() else self.root / p

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def is_dir(self, rel: str) -> bool:
        return self.path(rel).is_dir()

    def is_executable(self, rel: str) -> bool:
        return os.access(self.path(rel), os.X_OK)

    def read_text(self, rel: str) -> str | None:
        key = str(self.path(rel))
        if key not in self._cache:
            try:
                self._cache[key] = self.path(rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                self._cache[key] = None
        return self._cache[key]


def _read_env(path: Path) -> dict[str, str]:
    try:
        return env_dict(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProjectError(f"cannot read env file {path}: {exc.strerror}") from exc
    except EnvFileError as exc:
        raise ProjectError(f"{path}: {exc}") from exc


def default_env_file(root: Path) -> Path | None:
    """``.env`` if it exists (what compose reads), else ``example.env``."""
    for name in (".env", DEFAULT_EXAMPLE):
        if (root / name).is_file():
            return root / name
    return None


def load_project(
    root: str | Path = ".",
    compose_file: str = DEFAULT_COMPOSE,
    env_file: str | Path | None = None,
) -> Project:
    root = Path(root)
    if not root.is_dir():
        raise ProjectError(f"not a directory: {root}")
    compose_path = root / compose_file
    try:
        text = compose_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"cannot read compose file {compose_path}: {exc.strerror}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProjectError(f"{compose_path}: invalid YAML: {exc}") from exc

    env_path = Path(env_file) if env_file is not None else default_env_file(root)
    if env_path is not None and not env_path.is_absolute() and env_file is not None:
        env_path = env_path if env_path.exists() else root / env_path
    env = _read_env(env_path) if env_path is not None else {}

    example = root / DEFAULT_EXAMPLE
    example_env = None
    if example.is_file() and (env_path is None or example.resolve() != env_path.resolve()):
        example_env = _read_env(example)
    return Project(root, compose_path, data, env, env_path, example_env)
