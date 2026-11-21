"""Workspace file primitives; bounded reads and atomic, conflict-aware mutations."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from deep_harness.domain.models import DomainError

MAX_FILE_BYTES = 8 * 1024 * 1024
OUTPUT_CHARS = 24_000


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=200, ge=1, le=2000)
    column: int = Field(default=0, ge=0)


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
        raise DomainError("file_too_large", "File exceeds the 8 MiB file-tool limit")
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
        raise DomainError("file_too_large", "Result exceeds the 8 MiB file-tool limit")
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


def mutate(arguments: WriteArgs | EditArgs, workspace_path):
    target = workspace_path(arguments.path)
    current = read_bytes(target) if target.exists() else None
    digest = hashlib.sha256(current).hexdigest() if current is not None else None
    if arguments.expected_sha256 is not None and arguments.expected_sha256 != digest:
        raise DomainError("file_conflict", "File changed since the expected SHA-256 was read")
    if isinstance(arguments, EditArgs):
        if current is None:
            raise DomainError("not_file", "Cannot edit a missing file")
        text = text_content(current)
        first = text.find(arguments.old_text)
        if first == -1 or first != text.rfind(arguments.old_text):
            raise DomainError(
                "edit_match",
                f"Expected one exact match; found {'zero' if first == -1 else 'multiple'}. "
                "Read the file and provide unique context.",
            )
        data = text.replace(arguments.old_text, arguments.new_text, 1).encode("utf-8")
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
