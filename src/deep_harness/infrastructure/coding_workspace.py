"""Bounded working-tree evidence owned by a coding admission."""

import difflib
import hashlib
import json
import os
import stat
from pathlib import Path

from deep_harness.application.atomic_files import atomic_write
from deep_harness.application.permissions import sensitive_path
from deep_harness.application.workspace import SKIP_DIRECTORIES
from deep_harness.domain.identity import fingerprint
from deep_harness.infrastructure.git_workspace import visible_git_paths

MAX_ENTRIES = 12_000
MAX_SCAN_BYTES = 128 * 1024 * 1024
MAX_TEXT_BYTES = 512 * 1024
MAX_DIFF_CHARS = 80_000


def candidates(root, eligible, previous_paths, boundary):
    paths = visible_git_paths(root, boundary=boundary)
    if paths is not None:
        paths.update(root / name for name in previous_paths)
        yield from sorted(paths)
        return

    def walk_error(error):
        raise error

    for parent, dirs, files in os.walk(root, onerror=walk_error):
        directory = Path(parent)
        dirs[:] = sorted(name for name in dirs if eligible(directory / name))
        yield from (directory / name for name in sorted(files))


def capture(scope, *, include_text=False, previous_paths=(), include_instructions=False):
    root = scope.workspace
    files = {}
    unreadable = False
    scanned_bytes = text_bytes = path_bytes = 0

    def eligible(path):
        instructions = (
            include_instructions and path.name.lower() == "agents.md"
            and not sensitive_path(path.parent)
        )
        return (
            path.is_relative_to(root)
            and not set(path.relative_to(root).parts).intersection(SKIP_DIRECTORIES)
            and (instructions or not sensitive_path(path))
            and (scope.task_workspace or not path.is_relative_to(scope.state_root))
            and not any(path.is_relative_to(other) for other in scope.protected_paths)
            and path.resolve() == path
            and not path.is_junction()
        )

    try:
        boundary = root if scope.task_workspace else None
        for index, path in enumerate(candidates(root, eligible, previous_paths, boundary)):
            if index >= MAX_ENTRIES:
                unreadable = True
                break
            try:
                if not eligible(path):
                    continue
                name = path.relative_to(root).as_posix()
                path_bytes += len(json.dumps(name))
                if path_bytes > 1024 * 1024:
                    unreadable = True
                    break
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode):
                    continue
                remaining = MAX_SCAN_BYTES - scanned_bytes
                if before.st_size > remaining:
                    unreadable = True
                    continue
                with path.open("rb") as stream:
                    data = stream.read(before.st_size + 1)
                    after = os.fstat(stream.fileno())
                scanned_bytes += len(data)
                if len(data) != before.st_size or (
                    before.st_ino, before.st_mtime_ns, before.st_size
                ) != (after.st_ino, after.st_mtime_ns, after.st_size):
                    unreadable = True
                    continue
            except FileNotFoundError:
                # Git also lists tracked paths that were deleted from the working tree.
                continue
            except (OSError, RuntimeError):
                unreadable = True
                continue
            entry = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": stat.S_IMODE(before.st_mode),
            }
            if (
                include_text
                and len(data) <= min(32_768, MAX_TEXT_BYTES - text_bytes)
                and b"\0" not in data
            ):
                try:
                    entry["text"] = data.decode("utf-8")
                    text_bytes += len(data)
                except UnicodeDecodeError:
                    pass
            files[name] = entry
    except (OSError, RuntimeError):
        unreadable = True
    digest = fingerprint({name: (row["sha256"], row["mode"]) for name, row in files.items()})
    return {"files": files, "complete": not unreadable, "digest": digest}


def changes_between(baseline, final):
    if baseline is None:
        return {"status": "unavailable", "files": [], "diff": "", "diff_truncated": False}
    old, new = baseline["files"], final["files"]
    files, parts = [], []
    remaining = MAX_DIFF_CHARS
    truncated = False
    for name in sorted(old.keys() | new.keys()):
        before, after = old.get(name), new.get(name)
        if (before is None and not baseline["complete"]) or (
            after is None and not final["complete"]
        ):
            continue
        previous = before["sha256"] if before else None
        current = after["sha256"] if after else None
        if previous == current and before["mode"] == after["mode"]:
            continue
        files.append({
            "path": name, "before_sha256": previous, "sha256": current,
            "status": "added" if before is None else "deleted" if after is None else "modified",
        })
        left, right = before or {"text": ""}, after or {"text": ""}
        if "text" not in left or "text" not in right or previous == current:
            truncated = True
            continue
        lines = difflib.unified_diff(
            left["text"].splitlines(keepends=True), right["text"].splitlines(keepends=True),
            f"a/{name}" if before else "/dev/null", f"b/{name}" if after else "/dev/null",
        )
        for line in lines:
            if not line.endswith("\n"):
                line += "\n\\ No newline at end of file\n"
            if len(line) > remaining:
                truncated = True
                remaining = 0
                break
            parts.append(line)
            remaining -= len(line)
        if not left["text"] and not right["text"]:
            truncated = True
    return {
        "status": "observed" if baseline["complete"] and final["complete"] else "partial",
        "files": files, "diff": "".join(parts), "diff_truncated": truncated,
    }


class CodingWorkspace:
    def __init__(self, scope, admission_id):
        self.scope = scope
        base = scope.home / "coding-workspaces" / fingerprint(admission_id)
        self.baseline = base.with_suffix(".baseline.json")
        self.final = base.with_suffix(".final.json")

    def begin(self):
        if not self.baseline.exists():
            atomic_write(self.baseline, json.dumps(capture(self.scope, include_text=True)).encode())

    def read_baseline(self):
        return json.loads(self.baseline.read_bytes()) if self.baseline.exists() else None

    def revision(self):
        baseline = self.read_baseline()
        if baseline is None or not baseline["complete"]:
            return None
        state = capture(self.scope, previous_paths=baseline["files"])
        return state["digest"] if state["complete"] else None

    def finish(self):
        if self.final.exists():
            return json.loads(self.final.read_bytes())
        result = self.current_changes()
        if result["status"] != "unavailable":
            atomic_write(self.final, json.dumps(result).encode())
        return result

    def current_changes(self):
        baseline = self.read_baseline()
        if baseline is None:
            return {**changes_between(None, None), "revision": None}
        final = capture(self.scope, include_text=True, previous_paths=baseline["files"])
        result = changes_between(baseline, final)
        result["revision"] = final["digest"] if final["complete"] else None
        return result

    def review_snapshot(self, identifier):
        name = fingerprint([self.final.name, identifier])[:32] + ".json"
        path = self.final.parent / "reviews" / name
        if path.exists():
            return json.loads(path.read_bytes())
        change = self.current_changes()
        review = {"revision": change["revision"], "diff": change["diff"][:12_000]}
        atomic_write(path, json.dumps(review).encode())
        return review
