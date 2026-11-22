"""Workspace file primitives; bounded reads and atomic, conflict-aware mutations."""

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from deep_harness.domain.models import DomainError
from deep_harness.server_config import number

MAX_FILE_BYTES = number("files.max_bytes")
OUTPUT_CHARS = number("files.output_chars")


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)
    offset: int = Field(default=number("files.offset"), ge=1)
    limit: int = Field(default=number("files.limit"), ge=1, le=number("files.limit_max"))
    column: int = Field(default=number("files.column"), ge=0)


class WriteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)
    content: str = Field(max_length=MAX_FILE_BYTES)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1, max_length=MAX_FILE_BYTES)
    new_text: str = Field(max_length=MAX_FILE_BYTES)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def read_bytes(target: Path) -> bytes:
    if not target.is_file():
        raise DomainError("not_file", "Choose an existing regular UTF-8 file")
    with target.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise DomainError("file_too_large", "File exceeds the configured file-tool limit")
    if b"\x00" in data:
        raise DomainError("binary_file", "File tools accept UTF-8 text, not binary content")
    return data


def text_content(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise DomainError("text_encoding", "File must contain valid UTF-8 text") from exc


def read(arguments: ReadArgs, workspace_path, candidate_path=None):
    target = workspace_path(arguments.path)
    candidate_path = candidate_path or workspace_path
    if target.is_dir():
        # scandir avoids materializing an unbounded directory just to return one page.
        entries = []
        skipped = 0
        size = 0
        truncated = False
        with os.scandir(target) as children:
            for child in children:
                relative = (Path(arguments.path) / child.name).as_posix()
                try:
                    candidate_path(relative)
                except DomainError:
                    continue
                skipped += 1
                if skipped < arguments.offset:
                    continue
                size += len(child.name)
                if len(entries) >= arguments.limit or size > OUTPUT_CHARS:
                    truncated = True
                    break
                entries.append(
                    {"name": child.name, "type": "directory" if child.is_dir() else "file"}
                )
        return {
            "path": arguments.path,
            "entries": entries,
            "truncated": truncated,
            "next_offset": arguments.offset + len(entries) if truncated else None,
        }
    data = read_bytes(target)
    lines = text_content(data).splitlines(keepends=True)
    remaining = OUTPUT_CHARS
    parts = []
    line_index = arguments.offset - 1
    column = arguments.column
    end = min(len(lines), line_index + arguments.limit)
    while line_index < end and remaining:
        line = lines[line_index]
        if column > len(line):
            raise DomainError("read_column", "column exceeds the selected line length")
        part = line[column : column + remaining]
        parts.append(part)
        remaining -= len(part)
        column += len(part)
        if column < len(line):
            break
        line_index += 1
        column = 0
    truncated = line_index < len(lines)
    return {
        "path": arguments.path,
        "content": "".join(parts),
        "offset": arguments.offset,
        "total_lines": len(lines),
        "sha256": hashlib.sha256(data).hexdigest(),
        "truncated": truncated,
        "next_offset": line_index + 1 if truncated else None,
        "next_column": column if truncated else None,
    }


def atomic_write(target: Path, data: bytes):
    if len(data) > MAX_FILE_BYTES:
        raise DomainError("file_too_large", "Result exceeds the configured file-tool limit")
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def replace_text(text: str, old: str, new: str) -> str:
    start = text.find(old)
    if start > 0 and text[start - 1 : start + 1] == "\r\n":
        start = -1
    if start >= 0:
        if start != text.rfind(old):
            raise DomainError("edit_match", "Expected one exact match; found multiple.")
        end = start + len(old)
    else:
        normalized = text.replace("\r\n", "\n")
        needle = old.replace("\r\n", "\n")
        index = normalized.find(needle)
        if index < 0 or index != normalized.rfind(needle):
            raise DomainError(
                "edit_match", "Expected one match after newline normalization; read unique context."
            )
        # Solve for both raw offsets in one pass over CRLF positions.
        start, end = index, index + len(needle)
        for match in re.finditer("\r\n", text):
            if match.start() < start:
                start += 1
            if match.start() >= end:
                break
            end += 1
    if "\n" in new:
        region = text[start:end]
        styles = set(re.findall(r"\r?\n", region))
        if not styles:
            styles = set(re.findall(r"\r?\n", text))
        if len(styles) > 1:
            raise DomainError("edit_match", "Mixed newline styles; narrow the edit.")
        if styles:
            new = new.replace("\r\n", "\n").replace("\n", next(iter(styles)))
    return text[:start] + new + text[end:]


def mutate(arguments: WriteArgs | EditArgs, workspace_path):
    target = workspace_path(arguments.path)
    current = read_bytes(target) if target.exists() else None
    digest = hashlib.sha256(current).hexdigest() if current is not None else None
    if arguments.expected_sha256 is not None and arguments.expected_sha256 != digest:
        raise DomainError("file_conflict", "File changed since the expected SHA-256 was read")
    if isinstance(arguments, EditArgs):
        if current is None:
            raise DomainError("not_file", "Cannot edit a missing file")
        data = replace_text(text_content(current), arguments.old_text, arguments.new_text).encode(
            "utf-8"
        )
    else:
        if current is not None and arguments.expected_sha256 is None:
            raise DomainError("file_conflict", "Overwriting requires the SHA-256 from read")
        data = arguments.content.encode("utf-8")
    atomic_write(target, data)
    return {
        "path": arguments.path,
        "bytes_written": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
