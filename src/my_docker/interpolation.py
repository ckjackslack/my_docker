"""Docker Compose style ``${VAR}`` interpolation.

Supports ``$VAR``, ``${VAR}``, ``${VAR:-default}``, ``${VAR-default}``, ``${VAR:?error}``,
``${VAR?error}``, ``${VAR:+alt}``, ``${VAR+alt}``, nested references inside the argument and
``$$`` as an escaped literal ``$`` - the subset of the compose spec that real files use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPERATORS = (":-", ":?", ":+", "-", "?", "+")


class InterpolationError(ValueError):
    """Raised for malformed references or a missing ``${VAR:?}`` / ``${VAR?}`` value."""


@dataclass(frozen=True)
class VarRef:
    name: str
    op: str | None = None
    arg: str | None = None

    @property
    def required(self) -> bool:
        return self.op in (":?", "?")

    @property
    def has_fallback(self) -> bool:
        """True if an unset variable is fine (compose substitutes a default or nothing)."""
        return self.op in (":-", "-", ":+", "+")


def _matching_brace(text: str, start: int) -> int | None:
    depth, i = 1, start
    while i < len(text):
        if text.startswith("${", i):
            depth += 1
            i += 2
            continue
        if text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _scan(text: str, env: Mapping[str, str], refs: list[VarRef] | None, strict: bool) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if nxt == "$":
            out.append("$")
            i += 2
        elif nxt == "{":
            end = _matching_brace(text, i + 2)
            if end is None:
                raise InterpolationError(f"unterminated '${{' in {text!r}")
            out.append(_expand(text[i + 2 : end], env, refs, strict))
            i = end + 1
        else:
            m = _NAME.match(text, i + 1)
            if m:
                if refs is not None:
                    refs.append(VarRef(m.group(0)))
                out.append(env.get(m.group(0), ""))
                i = m.end()
            else:
                out.append("$")
                i += 1
    return "".join(out)


def _expand(body: str, env: Mapping[str, str], refs: list[VarRef] | None, strict: bool) -> str:
    m = _NAME.match(body)
    if not m:
        raise InterpolationError(f"invalid variable reference '${{{body}}}'")
    name, rest = m.group(0), body[m.end() :]
    if not rest:
        if refs is not None:
            refs.append(VarRef(name))
        return env.get(name, "")
    op = next((o for o in _OPERATORS if rest.startswith(o)), None)
    if op is None:
        raise InterpolationError(f"invalid variable reference '${{{body}}}'")
    raw_arg = rest[len(op) :]
    if refs is not None:
        refs.append(VarRef(name, op, raw_arg))
        _scan(raw_arg, env, refs, strict=False)  # record nested references

    value = env.get(name)
    is_set = value is not None
    non_empty = bool(value)

    def arg() -> str:
        return _scan(raw_arg, env, None, strict)

    if op == ":-":
        return value if non_empty else arg()  # type: ignore[return-value]
    if op == "-":
        return value if is_set else arg()  # type: ignore[return-value]
    if op == ":+":
        return arg() if non_empty else ""
    if op == "+":
        return arg() if is_set else ""
    # ":?" and "?"
    if (op == ":?" and not non_empty) or (op == "?" and not is_set):
        if strict:
            message = arg() or "not set"
            raise InterpolationError(f"required variable {name} is missing a value: {message}")
        return ""
    return value  # type: ignore[return-value]


def find_refs(text: str) -> list[VarRef]:
    """All variable references in ``text`` (including nested ones), in order."""
    refs: list[VarRef] = []
    _scan(text, {}, refs, strict=False)
    return refs


def interpolate(text: str, env: Mapping[str, str]) -> str:
    """Substitute references in ``text``; raises for missing required variables."""
    return _scan(text, env, None, strict=True)


def walk_strings(obj: Any):
    """Yield every string value in a nested dict/list structure (keys excluded)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_strings(value)


def interpolate_tree(obj: Any, env: Mapping[str, str]) -> Any:
    """Return a copy of a loaded YAML tree with every string value interpolated."""
    if isinstance(obj, str):
        return interpolate(obj, env)
    if isinstance(obj, dict):
        return {key: interpolate_tree(value, env) for key, value in obj.items()}
    if isinstance(obj, list):
        return [interpolate_tree(value, env) for value in obj]
    return obj
