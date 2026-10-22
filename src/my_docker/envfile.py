"""Reading compose ``.env`` files and rendering a real ``.env`` from ``example.env``."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Callable, Mapping

PLACEHOLDER = "change-me"
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\Z")
_SAFE_VALUE = re.compile(r"[A-Za-z0-9_./:@%+,=-]*\Z")


class EnvFileError(ValueError):
    def __init__(self, lineno: int, message: str):
        super().__init__(f"line {lineno}: {message}")
        self.lineno = lineno


@dataclass(frozen=True)
class EnvLine:
    raw: str
    key: str | None = None
    value: str | None = None


def _parse_value(raw: str, lineno: int) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        end = 1
        while end < len(raw):
            if quote == '"' and raw[end] == "\\":
                end += 2
                continue
            if raw[end] == quote:
                break
            end += 1
        else:
            raise EnvFileError(lineno, "unterminated quoted value")
        inner = raw[1:end]
        trailing = raw[end + 1 :].strip()
        if trailing and not trailing.startswith("#"):
            raise EnvFileError(lineno, f"unexpected text after quoted value: {trailing!r}")
        if quote == '"':
            inner = re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m[1], m[1]), inner)
        return inner
    # unquoted: an inline comment needs whitespace before '#'
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def parse_env(text: str) -> list[EnvLine]:
    """Parse env-file text into lines, keeping comments/blank lines for round-tripping."""
    lines: list[EnvLine] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(EnvLine(raw))
            continue
        body = stripped[len("export ") :].lstrip() if stripped.startswith("export ") else stripped
        if "=" not in body:
            raise EnvFileError(lineno, f"expected KEY=VALUE, got {stripped!r}")
        key, _, value = body.partition("=")
        key = key.strip()
        if not _KEY.match(key):
            raise EnvFileError(lineno, f"invalid variable name {key!r}")
        lines.append(EnvLine(raw, key, _parse_value(value, lineno)))
    return lines


def env_dict(text: str) -> dict[str, str]:
    return {line.key: line.value or "" for line in parse_env(text) if line.key}


def format_value(value: str) -> str:
    """Quote a value so docker compose reads it back verbatim (no interpolation)."""
    if _SAFE_VALUE.match(value):
        return value
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return '"' + escaped.replace("\n", "\\n") + '"'


def generate_secret(nbytes: int = 24) -> str:
    """URL/env-safe random secret (letters, digits, '-' and '_')."""
    return secrets.token_urlsafe(nbytes)


@dataclass(frozen=True)
class RenderResult:
    text: str
    generated: tuple[str, ...]
    overridden: tuple[str, ...]


def render_env(
    example_text: str,
    overrides: Mapping[str, str] | None = None,
    secret_factory: Callable[[], str] = generate_secret,
) -> RenderResult:
    """Render a ``.env`` from the example: placeholders become secrets, overrides win.

    Comments and ordering are preserved. Override keys not present in the example are
    appended at the end.
    """
    overrides = dict(overrides or {})
    out: list[str] = []
    generated: list[str] = []
    overridden: list[str] = []
    for line in parse_env(example_text):
        if line.key is None:
            out.append(line.raw)
        elif line.key in overrides:
            out.append(f"{line.key}={format_value(overrides.pop(line.key))}")
            overridden.append(line.key)
        elif (line.value or "").strip().lower() == PLACEHOLDER:
            out.append(f"{line.key}={format_value(secret_factory())}")
            generated.append(line.key)
        else:
            out.append(f"{line.key}={format_value(line.value or '')}")
    if overrides:
        out.extend(["", "# --- overrides ---"])
        for key, value in overrides.items():
            out.append(f"{key}={format_value(value)}")
            overridden.append(key)
    return RenderResult("\n".join(out) + "\n", tuple(generated), tuple(overridden))


def parse_assignment(text: str) -> tuple[str, str]:
    """Parse a ``KEY=VALUE`` command-line override."""
    key, sep, value = text.partition("=")
    key = key.strip()
    if not sep or not _KEY.match(key):
        raise ValueError(f"expected KEY=VALUE, got {text!r}")
    return key, value
