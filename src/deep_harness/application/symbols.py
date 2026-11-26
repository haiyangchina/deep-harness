"""Bounded, content-versioned Python symbol lookup."""

import ast
import base64
import binascii
import fnmatch
import json
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from deep_harness.application.workspace import SearchFilesArgs, git_visibility, search_entries
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError


class SymbolArgs(SearchFilesArgs):
    pattern: str = "*.py"
    max_results: int = Field(default=40, ge=1, le=100)


class NavigationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    operation: Literal["definition", "references", "symbols", "diagnostics"]
    line: int = Field(default=1, ge=1)
    character: int = Field(default=0, ge=0)
    query: str = Field(default="", max_length=512)


@lru_cache(maxsize=256)
def python_symbols(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        return ()
    result = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result.append((node.name, node.lineno, "definition"))
        elif isinstance(node, ast.Name):
            result.append((node.id, node.lineno, "reference"))
        elif isinstance(node, ast.Attribute):
            result.append((node.attr, node.lineno, "reference"))
    return tuple(result)


def symbols(arguments, workspace_path, candidate_path):
    root = workspace_path(arguments.path)
    workspace = workspace_path(".")
    if not root.is_dir():
        raise DomainError("not_directory", "Choose a workspace directory")
    identity = fingerprint(["symbols", str(root), arguments.pattern, arguments.query])
    position, offset = (), 0
    if arguments.cursor:
        try:
            token, parts, offset = json.loads(base64.urlsafe_b64decode(arguments.cursor))
            if (
                token != identity or not isinstance(parts, list)
                or not all(isinstance(part, str) and part for part in parts)
                or type(offset) is not int or offset < 0
            ):
                raise ValueError
            position = tuple(parts)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise DomainError("search_cursor", "Cursor does not match this symbol lookup") from exc
    matches = []
    scanned = visited = 0

    def page(cursor=None, index=0):
        data = json.dumps([identity, cursor, index]).encode()
        return {
            "matches": matches, "truncated": cursor is not None,
            "next_cursor": base64.urlsafe_b64encode(data).decode() if cursor else None,
        }

    entries = search_entries(
        root, (), position, candidate_path, workspace, git_visibility(root, workspace)
    )
    for current, name, is_directory in entries:
        if current < position:
            continue
        if visited >= 2000:
            return page(current)
        visited += 1
        if is_directory or not fnmatch.fnmatch("/".join(current), arguments.pattern):
            continue
        if scanned >= 200:
            return page(current)
        scanned += 1
        try:
            with candidate_path(name).open("rb") as stream:
                data = stream.read(65537)
        except (OSError, DomainError):
            continue
        if len(data) > 65536 or b"\0" in data:
            continue
        first = offset if current == position else 0
        for index, (symbol, line, kind) in enumerate(python_symbols(data)[first:], first):
            if arguments.query != symbol:
                continue
            if len(matches) == arguments.max_results:
                return page(current, index)
            matches.append({"path": name, "line": line, "kind": kind})
    return page()
