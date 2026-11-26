"""Prepare and reopen a session-owned project checkout."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path

from filelock import AsyncFileLock
from filelock import Timeout as LockTimeout

from deep_harness.application.atomic_files import atomic_write
from deep_harness.application.process_environment import command_environment
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError, TaskWorkspace
from deep_harness.infrastructure.coding_workspace import capture


def git(root, *arguments):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        env=command_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=40,
        check=True,
    ).stdout


async def finish_io(function, *args):
    """Keep the caller's ownership until an already-started filesystem step settles."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await asyncio.gather(operation, return_exceptions=True)
        raise


@asynccontextmanager
async def workspace_lease(root, *, wait_seconds=20):
    directory = Path(tempfile.gettempdir()) / "deep-harness-workspace-leases"
    directory.mkdir(exist_ok=True)
    key = fingerprint(os.path.normcase(str(root.resolve())))
    try:
        async with AsyncFileLock(directory / (key + ".lock"), timeout=wait_seconds):
            yield
    except LockTimeout as exc:
        raise DomainError("workspace_busy", "Another coding operation owns this workspace") from exc


class TaskWorkspaces:
    def __init__(self, project_scope):
        self.project = project_scope

    def home(self, task_id):
        return self.project.home / "coding-tasks" / fingerprint(task_id)[:32]

    def paused(self, task_id):
        return (self.home(task_id) / "pause").exists()

    def set_pause(self, task_id, paused):
        marker = self.home(task_id) / "pause"
        if paused:
            atomic_write(marker, b"paused")
        else:
            marker.unlink(missing_ok=True)

    def scope_for(self, workspace):
        if workspace.mode == "in_place":
            return self.project
        return replace(self.project, workspace=workspace.root, task_workspace=True)

    def open(self, task_id, requested, allow_discarded=False):
        source = self.project.workspace
        if requested == "in_place":
            return TaskWorkspace(task_id, requested, source, source)
        home = self.home(task_id)
        marker = home / "workspace.json"
        root = home / "tree"
        if marker.exists():
            row = json.loads(marker.read_bytes())
            if row["source"] != str(source) or row["root"] != str(root):
                raise DomainError("workspace_lost", "The task checkout identity has changed")
            if row["status"] == "discarded":
                if allow_discarded:
                    return TaskWorkspace(task_id, row["mode"], source, root, row["baseline_commit"])
                raise DomainError("workspace_lost", "This task workspace was discarded")
            if row["status"] != "ready":
                raise DomainError(
                    "workspace_unknown", "Workspace preparation requires reconciliation"
                )
            if not root.is_dir():
                raise DomainError("workspace_lost", "The task checkout is no longer available")
            if root.resolve() != root or root.is_symlink() or root.is_junction():
                raise DomainError("workspace_lost", "The task checkout path has been replaced")
            if row["mode"] == "worktree" and not (root / ".git").is_file():
                raise DomainError("workspace_lost", "The Git worktree registration is missing")
            return TaskWorkspace(task_id, row["mode"], source, root, row["baseline_commit"])

        commit = None
        if requested in {"worktree", "auto"}:
            try:
                top = Path(
                    os.fsdecode(git(source, "rev-parse", "--show-toplevel").strip())
                ).resolve()
                if top == source:
                    commit = git(source, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            if requested == "worktree" and commit is None:
                raise DomainError(
                    "workspace_git", "Select a Git repository root with an existing commit"
                )
        mode = "worktree" if commit is not None else "copy"
        inventory = capture(self.project, include_instructions=True) if mode == "copy" else None
        if inventory is not None and not inventory["complete"]:
            raise DomainError("workspace_incomplete", "The project exceeds the safe copy limits")
        record = asdict(TaskWorkspace(task_id, mode, source, root, commit))
        self.save(marker, record, "preparing")
        try:
            if mode == "worktree":
                hooks = home / "no-hooks"
                hooks.mkdir()
                git(
                    source,
                    "-c",
                    f"core.hooksPath={hooks}",
                    "worktree",
                    "add",
                    "--detach",
                    "--",
                    str(root),
                    commit,
                )
            else:
                root.mkdir()
                for name, previous in inventory["files"].items():
                    original = source / name
                    target = root / name
                    if (
                        original.resolve() != original
                        or file_digest(original) != previous["sha256"]
                    ):
                        raise DomainError(
                            "workspace_conflict", f"Project changed during copy: {name}"
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(original, target)
                    if file_digest(target) != previous["sha256"]:
                        raise DomainError(
                            "workspace_conflict", f"Project changed during copy: {name}"
                        )
            baseline = capture(
                self.scope_for(TaskWorkspace(task_id, mode, source, root, commit)),
                include_text=True,
            )
            atomic_write(home / "baseline.json", json.dumps(baseline).encode())
            self.save(marker, record, "ready")
        except (OSError, subprocess.SubprocessError, DomainError) as exc:
            raise DomainError(
                "workspace_unknown", "Workspace preparation stopped; inspect task state"
            ) from exc
        return TaskWorkspace(task_id, mode, source, root, commit)

    def discard(self, task):
        home = self.home(task.task_id)
        marker = home / "workspace.json"
        if task.root != home / "tree" or task.root.resolve() != task.root:
            raise DomainError("workspace_lost", "Unexpected task checkout identity")
        self.save(marker, asdict(task), "discarding")
        try:
            if task.mode == "worktree":
                git(task.source, "worktree", "remove", "--force", "--", str(task.root))
            else:
                shutil.rmtree(task.root)
            self.save(marker, asdict(task), "discarded")
        except (subprocess.SubprocessError, OSError) as exc:
            raise DomainError(
                "workspace_unknown", "Workspace cleanup needs reconciliation"
            ) from exc

    @staticmethod
    def save(marker, record, status):
        atomic_write(marker, json.dumps({**record, "status": status}, default=str).encode())


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()
