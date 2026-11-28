"""Coordinate session cleanup under the owning Profile."""

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from deep_harness.application.tools import LOCAL_READS
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.task_workspaces import finish_io, git, workspace_lease
from deep_harness.runtime.session_checkpoints import delete_session_threads

logger = logging.getLogger(__name__)


class SessionDeletion:
    def __init__(self, app, ledger):
        self.app = app
        self.ledger = ledger
        self.running = {}

    async def start(self, session_id):
        app = self.app
        try:
            previous = await self.status(session_id)
        except DomainError as exc:
            if exc.code != "deletion_not_found":
                raise
        else:
            if previous["status"] == "deleted" or session_id in self.running:
                return previous
        async with app.control:
            lock = app.locks.setdefault(session_id, asyncio.Lock())
            if lock.locked() or app.mutations - {asyncio.current_task()}:
                raise DomainError(
                    "session_deletion_blocked", "当前还有会话操作，请等待完成后重试",
                )
            async with lock:
                if await self.ledger.other_deletion(session_id):
                    raise DomainError(
                        "session_deletion_blocked", "请先完成此工作区中的另一条会话清理",
                    )
                status = await self.ledger.begin(
                    session_id, lambda tasks: finish_io(self.verify_workspaces, tasks),
                    LOCAL_READS - {"symbols", "code_navigation"},
                )
        if status == "deleting":
            self.schedule(session_id)
        return {"session_id": session_id, "status": status}

    async def status(self, session_id):
        row = await self.ledger.deletion(session_id)
        return self.ledger.deletion_state(row)

    def schedule(self, session_id):
        if session_id not in self.running:
            task = asyncio.create_task(self.run(session_id))
            self.running[session_id] = task
            self.app.track(task)

    async def run(self, session_id):
        try:
            row = await self.ledger.deletion(session_id)
            if row["deletion"] != "deleting":
                return
            plan = row["cleanup"]
            await delete_session_threads(self.app.saver, session_id, plan["tasks"])
            if self.app.executor is not None:
                for run_id in plan["runs"]:
                    await self.app.executor.finish(run_id)
            async with workspace_lease(self.app.scope.workspace):
                await finish_io(self.remove_files, plan)
            await self.ledger.finish(session_id, plan)
        except Exception as exc:
            logger.warning("Session deletion %s stopped: %s", session_id, type(exc).__name__)
            reason = exc.code if isinstance(exc, DomainError) else "cleanup_failed"
            await self.ledger.needs_cleanup(session_id, reason)
        finally:
            self.running.pop(session_id, None)

    def safe(self, path):
        root = self.app.scope.home
        if not path.is_relative_to(root) or path == root or path.resolve() != path:
            raise DomainError("cleanup_path", "Cleanup would leave the Profile directory")
        for part in (path, *path.parents):
            if part == root.parent:
                break
            if part.is_symlink() or part.is_junction():
                raise DomainError("cleanup_path", "Cleanup refuses links and junctions")
        return path

    def verify_workspaces(self, tasks):
        for task_id in tasks:
            home = self.safe(self.app.workspaces.home(task_id))
            process_rows = self.app.development.list(self.safe(home / "processes"))
            if any(row["status"] in {"running", "unknown"} for row in process_rows):
                raise DomainError("session_deletion_blocked", "Resolve development processes first")
            delivery = self.safe(home / "delivery.json")
            if delivery.is_file() and json.loads(delivery.read_bytes())["status"] in {
                "applying", "undoing",
            }:
                raise DomainError("session_deletion_blocked", "Reconcile pending delivery first")

    def remove_files(self, plan):
        self.verify_workspaces(plan["tasks"])
        scope = self.app.scope
        manager = self.app.workspaces
        for task_id in plan["tasks"]:
            home = self.safe(manager.home(task_id))
            marker = self.safe(home / "workspace.json")
            tree = self.safe(home / "tree")
            if marker.is_file():
                row = json.loads(marker.read_bytes())
                state = manager.state(task_id)
                if row["mode"] == "worktree":
                    if state in {"ready", "built"}:
                        manager.discard(manager.open(task_id, "worktree", allow_built=True))
                    elif state != "discarded":
                        entries = git(scope.workspace, "worktree", "list", "--porcelain", "-z")
                        registered = (
                            Path(entry[9:].decode("utf-8")).resolve()
                            for entry in entries.split(b"\0") if entry.startswith(b"worktree ")
                        )
                        if tree.exists() or tree in registered:
                            raise DomainError(
                                "workspace_unknown", "Confirm the interrupted checkout removal",
                            )
            elif tree.exists():
                raise DomainError("workspace_unknown", "Checkout ownership needs confirmation")
            if home.is_dir():
                shutil.rmtree(home)
        for run_id in plan["runs"]:
            name = fingerprint(run_id)
            for suffix in (".baseline.json", ".final.json"):
                record = self.safe(scope.home / "coding-workspaces" / (name + suffix))
                record.unlink(missing_ok=True)
        for artifact_id in plan["owned"]:
            if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
                raise DomainError("cleanup_path", "Invalid artifact identifier")
            for suffix in ("", ".part"):
                self.safe(scope.home / "artifacts" / (artifact_id + suffix)).unlink(missing_ok=True)
