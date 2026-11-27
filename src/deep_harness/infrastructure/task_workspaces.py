"""Prepare and reopen a session-owned project checkout."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from filelock import AsyncFileLock
from filelock import Timeout as LockTimeout

from deep_harness.application.atomic_files import atomic_write
from deep_harness.application.process_environment import command_environment
from deep_harness.application.shell import stop_tree
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError, TaskWorkspace
from deep_harness.infrastructure.coding_workspace import capture
from deep_harness.infrastructure.windows_job import WindowsJob

PREPARATION_TIMEOUT_SECONDS = 90


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
    return await finish_task(asyncio.create_task(asyncio.to_thread(function, *args)))


async def finish_task(operation):
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not operation.cancelled():
            operation.exception()
        raise


@asynccontextmanager
async def workspace_lease(root, *, wait_seconds=20):
    directory = Path(tempfile.gettempdir()) / "deep-harness-workspace-leases"
    directory.mkdir(exist_ok=True)
    key = fingerprint(os.path.normcase(str(root.resolve())))
    lock = AsyncFileLock(
        directory / (key + ".lock"), timeout=wait_seconds, run_in_executor=False,
    )
    try:
        await lock.acquire()
    except LockTimeout as exc:
        raise DomainError("workspace_busy", "Another coding operation owns this workspace") from exc
    try:
        yield
    finally:
        await lock.release()


async def run_workspace_process(module, payload):
    """Start a gated worker and keep ownership until its process tree is stopped."""
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt" else {"start_new_session": True}
    )
    startup = asyncio.create_task(asyncio.create_subprocess_exec(
        sys.executable, "-m", f"deep_harness.infrastructure.{module}",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=command_environment(), **options,
    ))
    job = None

    async def reap():
        try:
            process = await startup
        except OSError:
            return
        if job is not None:
            job.close()
            await process.wait()
        elif process.returncode is None or os.name != "nt":
            await stop_tree(process)

    try:
        process = await asyncio.shield(startup)
        if os.name == "nt":
            job = WindowsJob(process.pid)
        output, _ = await process.communicate(payload)
        if process.returncode != 0:
            raise DomainError("workspace_worker_failed", "Workspace worker stopped unexpectedly")
        return json.loads(output)
    finally:
        await finish_task(asyncio.create_task(reap()))


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

    async def prepare(self, task_id, requested, *, progress=None):
        if requested == "in_place":
            return self.open(task_id, requested)
        root = self.home(task_id) / "tree"
        if progress is not None:
            await progress("waiting")
        async with workspace_lease(root):
            phase = await finish_io(self.state, task_id)
            if phase not in {"missing", "built"}:
                return await finish_io(self.open, task_id, requested)
            if phase == "missing":
                async with workspace_lease(self.project.workspace):
                    if progress is not None:
                        await progress("preparing")
                    await self._worker("build", task_id, requested)
            if progress is not None:
                await progress("baseline")
            await self._worker("baseline", task_id, requested)
            task = await finish_io(self.open, task_id, requested)
            if progress is not None:
                await progress("ready", mode=task.mode)
            return task

    async def _worker(self, phase, task_id, requested):
        attempt = uuid4().hex
        payload = json.dumps({
            "scope": asdict(self.project), "phase": phase, "task_id": task_id,
            "mode": requested, "attempt": attempt,
        }, default=str).encode("utf-8")
        try:
            async with asyncio.timeout(PREPARATION_TIMEOUT_SECONDS):
                result = await run_workspace_process("workspace_worker", payload)
        except asyncio.CancelledError:
            if phase == "build":
                await finish_io(self.release_unpublished_copy, task_id, attempt)
            raise
        except TimeoutError:
            result = None
        except (OSError, ValueError, DomainError) as exc:
            raise DomainError("workspace_unknown", "Workspace worker stopped unexpectedly") from exc
        if result is None:
            retryable = phase == "baseline" or await finish_io(
                self.release_unpublished_copy, task_id, attempt,
            )
            code = "workspace_timeout" if retryable else "workspace_unknown"
            raise DomainError(code, "Workspace preparation timed out")
        if "error" in result:
            raise DomainError(result["error"], result["message"])
        return result["phase"]


    def release_unpublished_copy(self, task_id, attempt):
        """Forget only a stopped attempt that never published its copy."""
        home = self.home(task_id)
        marker = home / "workspace.json"
        root = home / "tree"
        status = self.state(task_id)
        if status in {"missing", "built"}:
            return True
        row = json.loads(marker.read_bytes())
        if (
            status == "preparing" and row["mode"] == "copy" and row.get("attempt") == attempt
            and not os.path.lexists(root)
        ):
            marker.unlink()
            return True
        return False

    def state(self, task_id):
        home = self.home(task_id)
        if home.resolve() != home:
            raise DomainError("workspace_lost", "The task checkout directory has been replaced")
        marker = home / "workspace.json"
        if not marker.exists():
            return "missing"
        row = json.loads(marker.read_bytes())
        if (
            row["source"] != str(self.project.workspace)
            or row["root"] != str(marker.parent / "tree")
            or row["task_id"] != task_id
        ):
            raise DomainError("workspace_lost", "The task checkout identity has changed")
        return row["status"]

    def open(self, task_id, requested, allow_discarded=False, *, allow_built=False):
        source = self.project.workspace
        if requested == "in_place":
            return TaskWorkspace(task_id, requested, source, source)
        home = self.home(task_id)
        root = home / "tree"
        status = self.state(task_id)
        if status not in {"ready", "discarded"} and not (allow_built and status == "built"):
            raise DomainError("workspace_unknown", "Workspace preparation requires reconciliation")
        row = json.loads((home / "workspace.json").read_bytes())
        if status == "discarded":
            if allow_discarded:
                return TaskWorkspace(task_id, row["mode"], source, root, row["baseline_commit"])
            raise DomainError("workspace_lost", "This task workspace was discarded")
        if not root.is_dir():
            raise DomainError("workspace_lost", "The task checkout is no longer available")
        if root.resolve() != root or root.is_symlink() or root.is_junction():
            raise DomainError("workspace_lost", "The task checkout path has been replaced")
        if row["mode"] == "worktree" and not (root / ".git").is_file():
            raise DomainError("workspace_lost", "The Git worktree registration is missing")
        return TaskWorkspace(task_id, row["mode"], source, root, row["baseline_commit"])

    def build(self, task_id, requested, attempt):
        source = self.project.workspace
        home = self.home(task_id)
        root = home / "tree"
        marker = home / "workspace.json"
        if self.state(task_id) != "missing":
            raise DomainError("workspace_unknown", "Workspace preparation requires reconciliation")
        commit = None
        if requested in {"worktree", "auto"}:
            try:
                top = Path(
                    os.fsdecode(git(source, "rev-parse", "--show-toplevel").strip())
                ).resolve()
                if top == source:
                    commit = git(source, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
            except subprocess.TimeoutExpired as exc:
                raise DomainError("workspace_timeout", "Git workspace discovery timed out") from exc
            except (OSError, ValueError, subprocess.CalledProcessError):
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
        self.save(marker, {**record, "attempt": attempt}, "preparing")
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
                staging = home / f"copy-{attempt}"
                staging.mkdir()
                for name, previous in inventory["files"].items():
                    original = source / name
                    target = staging / name
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
                staging.rename(root)
            self.save(marker, record, "built")
        except (OSError, subprocess.SubprocessError, DomainError) as exc:
            raise DomainError(
                "workspace_unknown", "Workspace preparation stopped; inspect task state"
            ) from exc

    def baseline(self, task_id, requested):
        task = self.open(task_id, requested, allow_built=True)
        home = self.home(task_id)
        baseline = capture(self.scope_for(task), include_text=True)
        atomic_write(home / "baseline.json", json.dumps(baseline).encode())
        self.save(home / "workspace.json", asdict(task), "ready")

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
