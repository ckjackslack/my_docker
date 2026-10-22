"""Minimal nginx configuration parser for sanity checks.

Understands directives, blocks, quoted strings, comments and ``${VAR}`` inside words (as used
by envsubst templates). It does not know directive semantics beyond what the checks query.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TEMPLATE_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


class NginxSyntaxError(ValueError):
    def __init__(self, lineno: int, message: str):
        super().__init__(f"line {lineno}: {message}")
        self.lineno = lineno


@dataclass
class Directive:
    name: str
    args: list[str]
    lineno: int
    block: list["Directive"] | None = None


def tokenize(text: str) -> list[tuple[str, int]]:
    tokens: list[tuple[str, int]] = []
    i, n, lineno = 0, len(text), 1
    while i < n:
        ch = text[i]
        if ch == "\n":
            lineno += 1
            i += 1
        elif ch.isspace():
            i += 1
        elif ch == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif ch in "{};":
            tokens.append((ch, lineno))
            i += 1
        elif ch in "'\"":
            end = i + 1
            while end < n and text[end] != ch:
                if text[end] == "\\":
                    end += 1
                end += 1
            if end >= n:
                raise NginxSyntaxError(lineno, "unterminated quoted string")
            tokens.append((text[i + 1 : end], lineno))
            lineno += text.count("\n", i, end)
            i = end + 1
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in ";{}#'\"":
                if text.startswith("${", i):
                    close = text.find("}", i)
                    if close == -1:
                        raise NginxSyntaxError(lineno, "unterminated '${'")
                    i = close + 1
                else:
                    i += 1
            tokens.append((text[start:i], lineno))
    return tokens


def parse(text: str) -> list[Directive]:
    """Parse into a tree of directives; raises NginxSyntaxError on structural problems."""
    tokens = tokenize(text)
    stack: list[list[Directive]] = [[]]
    current: list[str] = []
    current_line = 0
    for tok, lineno in tokens:
        if tok == ";":
            if not current:
                raise NginxSyntaxError(lineno, "unexpected ';'")
            stack[-1].append(Directive(current[0], current[1:], current_line))
            current = []
        elif tok == "{":
            if not current:
                raise NginxSyntaxError(lineno, "block without a directive name")
            directive = Directive(current[0], current[1:], current_line, block=[])
            stack[-1].append(directive)
            stack.append(directive.block)  # type: ignore[arg-type]
            current = []
        elif tok == "}":
            if current:
                raise NginxSyntaxError(
                    current_line, f"directive '{current[0]}' is not terminated by ';'"
                )
            if len(stack) == 1:
                raise NginxSyntaxError(lineno, "unexpected '}'")
            stack.pop()
        else:
            if not current:
                current_line = lineno
            current.append(tok)
    if current:
        raise NginxSyntaxError(current_line, f"directive '{current[0]}' is not terminated by ';'")
    if len(stack) != 1:
        raise NginxSyntaxError(tokens[-1][1] if tokens else 1, "unclosed '{' block")
    return stack[0]


def iter_directives(tree: list[Directive], name: str | None = None):
    for directive in tree:
        if name is None or directive.name == name:
            yield directive
        if directive.block:
            yield from iter_directives(directive.block, name)


def upstreams(tree: list[Directive]) -> dict[str, list[str]]:
    """``upstream`` name -> list of ``server`` addresses."""
    result: dict[str, list[str]] = {}
    for up in iter_directives(tree, "upstream"):
        servers = [d.args[0] for d in up.block or [] if d.name == "server" and d.args]
        result[up.args[0] if up.args else ""] = servers
    return result


def proxy_targets(tree: list[Directive]) -> list[str]:
    """Host[:port] part of every ``proxy_pass`` URL."""
    targets = []
    for d in iter_directives(tree, "proxy_pass"):
        if d.args:
            url = d.args[0]
            rest = url.split("://", 1)[-1]
            targets.append(rest.split("/", 1)[0])
    return targets


def listen_ports(tree: list[Directive]) -> set[str]:
    ports: set[str] = set()
    for d in iter_directives(tree, "listen"):
        if d.args:
            addr = d.args[0]
            ports.add(addr.rsplit(":", 1)[-1] if ":" in addr else addr)
    return ports


def template_vars(text: str) -> set[str]:
    """Upper-case ``${VAR}`` placeholders meant for envsubst (comments ignored)."""
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    return set(_TEMPLATE_VAR.findall(code))


def split_host_port(address: str, default_port: str = "80") -> tuple[str, str]:
    if address.startswith("unix:"):
        return address, ""
    host, sep, port = address.rpartition(":")
    if not sep:
        return address, default_port
    return host, port
