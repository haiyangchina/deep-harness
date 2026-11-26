"""Operator-owned local development commands with bounded log tails."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from deep_harness.application.atomic_files import atomic_write
from deep_harness.application.shell import BashArgs, bash, shell_command
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError


@dataclass
class Process:
    row: dict
    task: asyncio.Task


class DevelopmentProcesses:
    def __init__(self):
        self.running = {}

    @staticmethod
    def save(path, row):
        atomic_write(path, json.dumps(row, ensure_ascii=False).encode())

    def list(self, home):
        result = []
        for path in sorted(home.glob("*.json")):
            if running := self.running.get(path):
                result.append(dict(running.row))
                continue
            row = json.loads(path.read_bytes())
            if row["status"] == "running":
                row["status"] = "unknown"
            result.append(row)
        return result

    async def start(self, home, root, request_id, command, cwd, lifetime, *, executor=None):
        if not 1 <= lifetime <= 86400 or not 1 <= len(request_id) <= 128:
            raise DomainError("process_argument", "Invalid process request ID or lifetime")

        def resolve(raw):
            target = (root / raw).resolve()
            if not target.is_relative_to(root):
                raise DomainError("path_denied", "Use a directory in the task workspace")
            return target

        # This operator-only endpoint has its own lifetime bound.
        try:
            args = BashArgs(command=command, cwd=cwd)
        except ValidationError as exc:
            raise DomainError(
                "process_argument", "Invalid development command or directory"
            ) from exc
        args = args.model_copy(update={"timeout_seconds": lifetime})
        if executor is None:
            await asyncio.to_thread(shell_command, args, resolve)
        else:
            executor.validate(args, resolve)
        key = fingerprint(request_id)
        path = home / (key[:32] + ".json")
        if path.exists():
            old = self.running[path].row if path in self.running else json.loads(path.read_bytes())
            if (old["id"], old["command"], old["cwd"], old["lifetime"]) != (
                key, command, cwd, lifetime
            ):
                raise DomainError("idempotency_conflict", "Process request parameters changed")
            return (
                {**old, "status": "unknown"}
                if path not in self.running and (old["status"] == "running")
                else dict(old)
            )
        if len(list(home.glob("*.json"))) >= 64 or len(self.running) >= 8:
            raise DomainError("process_limit", "Development process capacity reached")
        row = {
            "id": key,
            "command": command,
            "cwd": cwd,
            "lifetime": lifetime,
            "status": "running",
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
        self.save(path, row)
        task = asyncio.create_task(self.run(path, row, args, root, resolve, executor))
        self.running[path] = Process(row, task)
        return dict(row)

    async def run(self, path, row, args, root, resolve, executor):
        async def output(channel, text):
            row[channel] = (row[channel] + text)[-64_000:]

        try:
            if executor is None:
                result = await bash(
                    args, resolve, on_output=output, live=True, git_ceiling=root.parent
                )
            else:
                result = await executor.run_managed(args, resolve, root, on_output=output)
            row.update(status="exited", exit_code=result["exit_code"])
        except asyncio.CancelledError:
            row["status"] = "stopped"
        except TimeoutError:
            row["status"] = "expired"
        except (OSError, DomainError):
            row["status"] = "unknown"
        finally:
            try:
                await asyncio.to_thread(self.save, path, row)
            finally:
                self.running.pop(path, None)

    async def stop(self, home: Path, identifier):
        if len(identifier) != 64 or any(char not in "0123456789abcdef" for char in identifier):
            raise DomainError("process_not_found", "Development process is not accessible")
        path = home / (identifier[:32] + ".json")
        if running := self.running.get(path):
            # Allow a newly scheduled coroutine to reach its cleanup handler.
            await asyncio.sleep(0)
            running.task.cancel()
            await asyncio.gather(running.task, return_exceptions=True)
        if not path.exists():
            raise DomainError("process_not_found", "Development process is not accessible")
        row = json.loads(path.read_bytes())
        if row["id"] != identifier:
            raise DomainError("process_not_found", "Development process is not accessible")
        if row["status"] == "running":
            raise DomainError("unknown_effect", "Process ownership was lost; inspect it manually")
        return row

    async def close(self):
        await asyncio.gather(
            *(self.stop(path.parent, process.row["id"]) for path, process in self.running.items()),
            return_exceptions=True,
        )
