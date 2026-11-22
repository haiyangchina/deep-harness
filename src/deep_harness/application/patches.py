"""Strict unified hunks for approved UTF-8 file updates."""

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field

from deep_harness.application.atomic_files import MAX_FILE_BYTES, read_bytes, text_content
from deep_harness.domain.models import DomainError
from deep_harness.server_config import number

HUNK = re.compile(r"^@@ -(\d{1,9})(?:,(\d{1,9}))? \+(\d{1,9})(?:,(\d{1,9}))? @@(?: .*)?$")
NO_NEWLINE = r"\ No newline at end of file"


class FilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1, max_length=4096)
    expected_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    diff: str = Field(min_length=1, max_length=number("patch.max_file_diff_chars"))


class PatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    files: list[FilePatch] = Field(min_length=2, max_length=number("patch.max_files"))


def apply_hunks(source, diff, path, *, creating):
    lines = source.split("\n")
    source_lines = [line + "\n" for line in lines[:-1]]
    if lines[-1]:
        source_lines.append(lines[-1])
    rows = diff.replace("\r\n", "\n").split("\n")
    if rows[-1] == "":
        rows.pop()
    if rows and rows[0].startswith("--- "):
        name = path.replace("\\", "/")
        previous = {"/dev/null"} if creating else {name, f"a/{name}"}
        if (
            len(rows) < 3
            or rows[0][4:].partition("\t")[0] not in previous
            or not rows[1].startswith("+++ ")
            or rows[1][4:].partition("\t")[0] not in {name, f"b/{name}"}
        ):
            raise DomainError("patch_format", f"Unified file headers do not match {path}")
        rows = rows[2:]
    output = []
    position = 0
    index = 0
    delta = 0
    while index < len(rows):
        header = HUNK.fullmatch(rows[index])
        if header is None:
            raise DomainError("patch_format", "Expected a unified hunk header")
        old_start, old_count, new_start, new_count = header.groups()
        old_count = int(old_count or 1)
        new_count = int(new_count or 1)
        start = int(old_start) - bool(old_count)
        new_index = int(new_start) - bool(new_count)
        if not position <= start <= len(source_lines) or new_index != start + delta:
            raise DomainError("patch_format", "Hunk offsets do not match the file")
        output.extend(source_lines[position:start])
        position = start
        index += 1
        removed = added = 0
        while index < len(rows) and not rows[index].startswith("@@"):
            row = rows[index]
            if not row or row[0] not in " +-":
                raise DomainError("patch_format", "Invalid hunk line")
            marker, content = row[0], row[1:] + "\n"
            index += 1
            if index < len(rows) and rows[index] == NO_NEWLINE:
                content = content[:-1]
                index += 1
            if marker in " -":
                if position >= len(source_lines) or source_lines[position] != content:
                    raise DomainError("patch_context", "Hunk context does not match the baseline")
                position += 1
                removed += 1
            if marker in " +":
                output.append(content)
                added += 1
        if (removed, added) != (old_count, new_count):
            raise DomainError("patch_format", "Hunk line counts do not match its header")
        delta += added - removed
    output.extend(source_lines[position:])
    if any(not line.endswith("\n") for line in output[:-1]):
        raise DomainError("patch_format", "A no-newline marker must end the file")
    return "".join(output)


def prepare_file(target, patch):
    original = read_bytes(target) if target.exists() else None
    baseline = hashlib.sha256(original).hexdigest() if original is not None else None
    if baseline != patch.expected_sha256:
        raise DomainError(
            "file_conflict",
            f"Patch baseline does not match {patch.path}. Read that file and copy its SHA-256.",
        )
    source = text_content(original or b"")
    normalized = source.replace("\r\n", "\n")
    has_crlf = source != normalized
    if "\r" in normalized or (has_crlf and "\n" in source.replace("\r\n", "")):
        raise DomainError("patch_context", "Patch requires a consistent LF or CRLF file")
    result = apply_hunks(normalized, patch.diff, patch.path, creating=original is None)
    data = (result.replace("\n", "\r\n") if has_crlf else result).encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise DomainError("file_too_large", f"Patch result exceeds file limit for {patch.path}")
    if data == original:
        raise DomainError("patch_context", f"Patch does not change {patch.path}")
    return data
