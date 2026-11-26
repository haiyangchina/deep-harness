"""Disposable Docker containers owned by individual admissions."""

import asyncio
import os
import re
from uuid import uuid4

from deep_harness.application.shell import drain_output
from deep_harness.domain.models import DomainError

INITIALIZE = "set -eu; cp -R /source/. /task/; touch /tmp/harness-ready; exec sleep 86400"
EXECUTE = 'cd "/task/$1" || exit 2; exec /bin/sh -c "$2"'


async def capture(*args, limit_seconds=10, on_output=None):
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async def output(channel, text):
        await on_output(channel, text)

    readers = asyncio.gather(
        drain_output(
            process.stdout, (lambda text: output("stdout", text)) if on_output else None,
            live=on_output is not None,
        ),
        drain_output(
            process.stderr, (lambda text: output("stderr", text)) if on_output else None,
            live=on_output is not None,
        ),
    )
    try:
        async with asyncio.timeout(limit_seconds):
            stdout, stderr = await asyncio.shield(readers)
            code = await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        await asyncio.gather(readers, return_exceptions=True)
    return code, stdout, stderr


class DockerExecutor:
    def __init__(self, settings, owner=None):
        self.settings = settings
        self.label = f"local.deepharness.owner={owner}" if owner is not None else None
        self.workspaces = {}
        self.lock = asyncio.Lock()
        self.image_id = None
        self.managed_slots = asyncio.Semaphore(settings.max_workspaces)

    async def clean_orphans(self):
        if self.label is None:
            return
        code, output, _ = await capture(
            "ps", "-aq", "--filter", f"label={self.label}", limit_seconds=15
        )
        values = output[0].split()
        if (
            code
            or output[1]
            or any(not re.fullmatch(r"[0-9a-f]{12,64}", value) for value in values)
        ):
            raise DomainError("sandbox_unavailable", "Could not inspect owned containers")
        if values:
            code, _, _ = await capture("rm", "-f", *values, limit_seconds=15)
            if code:
                raise DomainError("sandbox_unavailable", "Could not clean owned containers")

    async def check(self):
        try:
            code, result, _ = await capture(
                "image", "inspect", "--format", "{{.Os}} {{.Id}}", self.settings.image
            )
        except (OSError, TimeoutError) as exc:
            raise DomainError(
                "sandbox_unavailable", "The configured Docker image is unavailable"
            ) from exc
        fields = result[0].strip().split()
        if code or len(fields) != 2 or fields[0] != "linux":
            raise DomainError("sandbox_unavailable", "Use a locally available Linux Docker image")
        self.image_id = fields[1]

    def validate(self, arguments, resolve):
        if arguments.shell == "powershell":
            raise DomainError("shell_unavailable", "The server sandbox uses a POSIX shell")
        resolve(arguments.cwd)

    def argv(self, name, workspace):
        if "," in str(workspace):
            raise DomainError("command_cwd", "Workspace path is unsupported by the executor")
        memory = f"{self.settings.memory_mb}m"
        labels = ("--label", self.label) if self.label else ()
        return (
            "run",
            "--detach",
            "--init",
            "--rm",
            "--pull",
            "never",
            "--name",
            name,
            *labels,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--pids-limit",
            str(self.settings.pids),
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            str(self.settings.cpus),
            "--mount",
            f"type=bind,source={workspace},target=/source,readonly,bind-recursive=disabled",
            "--tmpfs",
            "/tmp:rw,nodev,nosuid,noexec,size=16m",
            "--tmpfs",
            f"/task:rw,nodev,nosuid,size={self.settings.memory_mb // 2}m,mode=1777",
            "--entrypoint",
            "/bin/sh",
            self.image_id,
            "-c",
            INITIALIZE,
        )

    async def prepare(self, run_id, workspace, *, previously_used=False):
        async with self.lock:
            if run_id in self.workspaces:
                name = self.workspaces[run_id]
                code, _, _ = await capture("exec", name, "test", "-f", "/tmp/harness-ready")
                if code:
                    raise DomainError(
                        "sandbox_state_lost", "The task workspace is no longer active"
                    )
                return name
            if previously_used:
                raise DomainError(
                    "sandbox_state_lost", "Start a new task after the sandbox workspace was lost"
                )
            if len(self.workspaces) >= self.settings.max_workspaces:
                raise DomainError("sandbox_capacity", "Too many task workspaces are open")
            if self.image_id is None:
                await self.check()
            name = f"deep-harness-{uuid4().hex}"
            try:
                code, _, _ = await capture(*self.argv(name, workspace))
                if code:
                    raise DomainError("sandbox_unavailable", "Could not start the task container")
                async with asyncio.timeout(10):
                    while True:
                        code, _, _ = await capture("exec", name, "test", "-f", "/tmp/harness-ready")
                        if code == 0:
                            break
                        running, _, _ = await capture("inspect", name)
                        if running:
                            raise DomainError("sandbox_unavailable", "Could not copy the workspace")
                        await asyncio.sleep(0.05)
            except BaseException:
                await capture("rm", "-f", name)
                raise
            self.workspaces[run_id] = name
            return name

    async def finish(self, run_id):
        async with self.lock:
            name = self.workspaces.get(run_id)
            if name is not None:
                await capture("rm", "-f", name)
                self.workspaces.pop(run_id, None)

    async def close(self):
        for run_id in tuple(self.workspaces):
            await self.finish(run_id)

    async def run_managed(self, arguments, resolve, workspace, *, on_output=None):
        """Run a command against the same task tree used by the host file gateway."""
        async with self.managed_slots:
            return await self._run_mounted(arguments, resolve, workspace, on_output)

    async def _run_mounted(self, arguments, resolve, workspace, on_output):
        self.validate(arguments, resolve)
        if "," in str(workspace):
            raise DomainError("command_cwd", "Workspace path is unsupported by the executor")
        if self.image_id is None:
            await self.check()
        cwd = resolve(arguments.cwd).relative_to(workspace).as_posix()
        name = f"deep-harness-{uuid4().hex}"
        owner = f"{os.getuid()}:{os.getgid()}" if os.name != "nt" else "0:0"
        argv = self.argv(name, workspace)
        # Retain the existing resource and isolation options, replacing the source and task mounts.
        flags = list(argv[4:argv.index("--mount")])
        flags[flags.index("--user") + 1] = owner
        try:
            code, stdout, stderr = await capture(
                "run", "--rm", "--init", *flags,
                "--mount", f"type=bind,source={workspace},target=/task,bind-recursive=disabled",
                "--tmpfs", "/tmp:rw,nodev,nosuid,noexec,size=16m",
                "--entrypoint", "/bin/sh", self.image_id,
                "-c", EXECUTE, "harness", cwd, arguments.command,
                limit_seconds=arguments.timeout_seconds,
                on_output=on_output,
            )
            return {
                "exit_code": code, "stdout": stdout[0], "stderr": stderr[0],
                "stdout_truncated": stdout[1], "stderr_truncated": stderr[1], "sandbox": True,
            }
        finally:
            await capture("rm", "-f", name)

    async def run(self, arguments, resolve, workspace, *, run_id=None):
        self.validate(arguments, resolve)
        temporary = run_id is None
        run_id = run_id or uuid4().hex
        cwd = resolve(arguments.cwd).relative_to(workspace).as_posix()
        name = self.workspaces.get(run_id) or await self.prepare(run_id, workspace)
        try:
            code, stdout, stderr = await capture(
                "exec",
                name,
                "/bin/sh",
                "-c",
                EXECUTE,
                "harness",
                cwd,
                arguments.command,
                limit_seconds=arguments.timeout_seconds,
            )
            diff_code, diff, _ = await capture("exec", name, "diff", "-ruN", "/source", "/task")
            if diff_code not in {0, 1}:
                raise DomainError("unknown_effect", "Sandbox could not confirm the workspace diff")
            return {
                "exit_code": code,
                "stdout": stdout[0],
                "stderr": stderr[0],
                "stdout_truncated": stdout[1],
                "stderr_truncated": stderr[1],
                "workspace_diff": diff[0],
                "diff_truncated": diff[1],
                "sandbox": True,
            }
        except (TimeoutError, asyncio.CancelledError):
            await self.finish(run_id)
            raise
        finally:
            if temporary:
                await self.finish(run_id)
