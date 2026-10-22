from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STACK_FILES = [
    "docker-compose.local.yml",
    "Dockerfile",
    "Dockerfile_frontend",
    "example.env",
    "init-script.sh",
    "nginx.conf",
    "app",
]


@pytest.fixture
def repo() -> Path:
    return REPO


class Stack:
    """A throw-away copy of the real stack that tests can mutate."""

    def __init__(self, root: Path):
        self.root = root
        self.compose_path = root / "docker-compose.local.yml"

    def compose(self) -> dict:
        return yaml.safe_load(self.compose_path.read_text())

    def write_compose(self, data: dict) -> None:
        self.compose_path.write_text(yaml.safe_dump(data, sort_keys=False))

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text()
        assert old in text, f"{old!r} not in {rel}"
        path.write_text(text.replace(old, new))


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    root = tmp_path / "stack"
    root.mkdir()
    for name in STACK_FILES:
        src = REPO / name
        if src.is_dir():
            shutil.copytree(src, root / name, ignore=shutil.ignore_patterns("__pycache__", "staticfiles"))
        else:
            shutil.copy2(src, root / name)
    (root / "frontend").mkdir()
    (root / "frontend" / "package.json").write_text('{"scripts": {"serve": "true"}}\n')
    return Stack(root)


@pytest.fixture
def original(tmp_path: Path) -> Path:
    """The untouched 2024 prototype (compose, env, Dockerfile, nginx.conf)."""
    root = tmp_path / "original"
    shutil.copytree(FIXTURES / "original", root)
    return root
