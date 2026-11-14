"""Explicit shell commands with bounded output and owned process-tree cleanup."""

import asyncio
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from deep_harness.application.process_environment import command_environment
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.windows_job import WindowsJob


class BashArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(min_length=1, max_length=32_000)
    cwd: str = Field(default=".", min_length=1)
    timeout_seconds: float = Field(default=30, ge=1, le=120)
    shell: Literal["auto", "bash", "powershell"] = "auto"


def shell_kind(kind):
    return ("powershell" if os.name == "nt" else "bash") if kind == "auto" else kind


def shell_command(arguments, workspace_path):
    cwd = workspace_path(arguments.cwd)
    if not cwd.is_dir():
        raise DomainError("command_cwd", "Command working directory does not exist")
    if "\x00" in arguments.command:
        raise DomainError("command_argument", "Command contains a NUL character")
    kind = shell_kind(arguments.shell)
    candidates = ("pwsh", "powershell") if kind == "powershell" else ("bash",)
    executable = next((found for name in candidates if (found := shutil.which(name))), None)
    # Windows exposes a WSL launcher as bash.exe, which is not a native workspace shell.
    if kind == "bash" and os.name == "nt" and executable:
        if Path(executable).parent.name.lower() == "system32":
            raise DomainError("shell_unavailable", "Use PowerShell or put Git Bash on PATH")
    if executable is None:
        raise DomainError("shell_unavailable", f"{kind} is not installed or not on PATH")
    flags = (
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
        if kind == "powershell"
        else ["--noprofile", "--norc", "-c"]
    )
    command = arguments.command
    if kind == "powershell":
        command = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n"
            "$ErrorActionPreference = 'Stop'\n"
            + command
            + "\nif (-not $?) { exit 1 }; exit $LASTEXITCODE"
        )
    return [executable, *flags, command], cwd


async def drain_output(stream):
    data = bytearray()
    total = 0
    while chunk := await stream.read(8192):
        total += len(chunk)
        data.extend(chunk[: max(0, 65_536 - len(data))])
    return data.decode("utf-8", errors="replace"), total > len(data)


async def stop_tree(process):
    if os.name == "nt":
        # taskkill /T covers shell-launched children as well as the shell itself.
        killer = await asyncio.create_subprocess_exec(
            str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"),
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        process.kill()
    await process.wait()


async def bash(arguments: BashArgs, workspace_path):
    argv, cwd = shell_command(arguments, workspace_path)
    environment = command_environment()
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )
    readers = asyncio.gather(drain_output(process.stdout), drain_output(process.stderr))
    job = None
    try:
        if os.name == "nt":
            job = WindowsJob(process.pid)
        async with asyncio.timeout(arguments.timeout_seconds):
            stdout, stderr = await asyncio.shield(readers)
            code = await process.wait()
    finally:
        # Also reap descendants left behind after a successful shell exit.
        if job is not None:
            job.close()
            await process.wait()
        else:
            await stop_tree(process)
        await readers
    return {
        "exit_code": code,
        "stdout": stdout[0],
        "stderr": stderr[0],
        "stdout_truncated": stdout[1],
        "stderr_truncated": stderr[1],
    }
