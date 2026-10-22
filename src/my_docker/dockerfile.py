"""Just enough Dockerfile parsing for static checks (instructions, ARGs, COPY sources)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from my_docker.interpolation import interpolate, InterpolationError


@dataclass(frozen=True)
class Instruction:
    keyword: str
    args: str
    lineno: int


def parse_dockerfile(text: str) -> list[Instruction]:
    """Split a Dockerfile into instructions, joining ``\\`` continuations, dropping comments."""
    instructions: list[Instruction] = []
    buffer: list[str] = []
    start = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not buffer and (not line or line.startswith("#")):
            continue
        if buffer and line.startswith("#"):
            continue  # comment lines inside a continuation are ignored by docker
        if not buffer:
            start = lineno
        if line.endswith("\\"):
            buffer.append(line[:-1].strip())
            continue
        buffer.append(line)
        joined = " ".join(part for part in buffer if part)
        buffer = []
        keyword, _, args = joined.partition(" ")
        instructions.append(Instruction(keyword.upper(), args.strip(), start))
    if buffer:
        joined = " ".join(buffer)
        keyword, _, args = joined.partition(" ")
        instructions.append(Instruction(keyword.upper(), args.strip(), start))
    return instructions


def arg_values(instructions: list[Instruction], build_args: Mapping[str, str]) -> dict[str, str]:
    """ARG defaults overridden by compose build args (ARG scoping is not modelled)."""
    values: dict[str, str] = {}
    for ins in instructions:
        if ins.keyword == "ARG":
            name, sep, default = ins.args.partition("=")
            values[name.strip()] = default.strip().strip('"') if sep else ""
    values.update(build_args)
    return values


def _resolve(text: str, values: Mapping[str, str]) -> str:
    try:
        return interpolate(text, values)
    except InterpolationError:
        return text


def base_images(instructions: list[Instruction], values: Mapping[str, str]) -> list[str]:
    images = []
    for ins in instructions:
        if ins.keyword == "FROM":
            words = [w for w in ins.args.split() if not w.startswith("--")]
            if words:
                images.append(_resolve(words[0], values))
    return images


def copy_sources(instructions: list[Instruction], values: Mapping[str, str]) -> list[str]:
    """Context-relative COPY/ADD sources (skips ``--from`` copies, URLs and JSON form)."""
    sources: list[str] = []
    for ins in instructions:
        if ins.keyword not in ("COPY", "ADD") or ins.args.startswith("["):
            continue
        words = ins.args.split()
        if any(w.startswith("--from") for w in words):
            continue
        paths = [w for w in words if not w.startswith("--")][:-1]
        for path in paths:
            if "://" not in path:
                sources.append(_resolve(path, values))
    return sources


def has_instruction(instructions: list[Instruction], *keywords: str) -> bool:
    return any(ins.keyword in keywords for ins in instructions)


def exposed_ports(instructions: list[Instruction], values: Mapping[str, str]) -> set[str]:
    ports: set[str] = set()
    for ins in instructions:
        if ins.keyword == "EXPOSE":
            ports.update(_resolve(w, values).split("/")[0] for w in ins.args.split())
    return ports


def is_unpinned(image: str) -> bool:
    """True for images without a tag/digest or tagged ``latest``."""
    if "@" in image:
        return False
    last = image.rsplit("/", 1)[-1]
    return ":" not in last or last.endswith(":latest")
