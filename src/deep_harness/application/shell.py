"""Explicit shell commands with bounded output and owned process-tree cleanup."""

import asyncio
import base64
import codecs
import ctypes
import os
import shutil
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from deep_harness.application.process_environment import command_environment
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.windows_job import WindowsJob
from deep_harness.server_config import number


class BashArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str = Field(min_length=1, max_length=number("shell.command_max_length"))
    cwd: str = Field(default=".", min_length=1)
    timeout_seconds: float = Field(
        default=number("shell.timeout_seconds"), ge=1, le=number("shell.timeout_seconds_max")
    )
    shell: Literal["auto", "bash", "powershell"] = "auto"
    purpose: Literal["general", "reproduce", "verify"] = Field(
        default="general",
        description="Use reproduce for baseline tests and verify for tests after changes.",
    )


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
    payload = None
    if kind == "powershell":
        # Use stdin so encoding does not expand the command beyond the OS argv limit.
        payload = base64.b64encode(command.encode("utf-8"))
        # Parse user syntax only after the console can emit UTF-8 diagnostics.
        command = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n"
            "$OutputEncoding = [Console]::OutputEncoding\n"
            "$ErrorActionPreference = 'Stop'\n"
            "$commandBytes = [Convert]::FromBase64String([Console]::In.ReadToEnd())\n"
            ". ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString($commandBytes)))\n"
            "if (-not $?) { exit 1 }; exit $LASTEXITCODE"
        )
    return [executable, *flags, command], cwd, payload


def windows_output_encodings():
    if os.name != "nt":
        return ()
    kernel = ctypes.windll.kernel32
    codepages = dict.fromkeys((kernel.GetOEMCP(), kernel.GetACP()))
    return tuple(f"cp{page}" for page in codepages if page and page != 65001)


def decode_output(data, decoder, fallback_encodings, *, final=False):
    pending = decoder.getstate()[0]
    try:
        return decoder.decode(data, final=final), decoder
    except UnicodeDecodeError:
        # Keep any bytes buffered at a pipe boundary when switching encodings.
        data = pending + data
    for encoding in fallback_encodings:
        try:
            candidate = codecs.getincrementaldecoder(encoding)(errors="strict")
            return candidate.decode(data, final=final), candidate
        except (LookupError, UnicodeDecodeError):
            continue
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    return decoder.decode(data, final=final), decoder


async def drain_output(stream, on_output=None, *, live=False, fallback_encodings=()):
    data = bytearray()
    total = 0
    previews = 0
    utf8 = codecs.getincrementaldecoder("utf-8")
    errors = "strict" if fallback_encodings else "replace"
    decoder = utf8(errors=errors)
    while chunk := await stream.read(number("streams.read_chunk_bytes")):
        if on_output is not None and (live or (total < 4096 and previews < 8)):
            preview, decoder = decode_output(
                chunk if live else chunk[: 4096 - total], decoder, fallback_encodings
            )
            if preview:
                await on_output(preview)
                previews += 1
        total += len(chunk)
        data.extend(chunk[: max(0, number("shell.stream_limit_bytes") - len(data))])
    if on_output is not None and (live or (total <= 4096 and previews < 8)):
        tail, _ = decode_output(b"", decoder, fallback_encodings, final=True)
        if tail:
            await on_output(tail)
    truncated = total > len(data)
    output, _ = decode_output(
        bytes(data), utf8(errors=errors), fallback_encodings, final=not truncated
    )
    return output, truncated


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


async def bash(
    arguments: BashArgs, workspace_path, *, on_output=None, live=False, git_ceiling=None
):
    argv, cwd, payload = shell_command(arguments, workspace_path)
    environment = command_environment()
    if git_ceiling is not None:
        environment["GIT_CEILING_DIRECTORIES"] = str(git_ceiling)
    options = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL if payload is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **options,
    )
    async def output(channel, text):
        await on_output(channel, text)

    fallback_encodings = windows_output_encodings()
    readers = asyncio.gather(
        drain_output(
            process.stdout,
            (lambda text: output("stdout", text)) if on_output else None,
            live=live,
            fallback_encodings=fallback_encodings,
        ),
        drain_output(
            process.stderr,
            (lambda text: output("stderr", text)) if on_output else None,
            live=live,
            fallback_encodings=fallback_encodings,
        ),
    )
    job = None
    try:
        if os.name == "nt":
            job = WindowsJob(process.pid)
        async with asyncio.timeout(arguments.timeout_seconds):
            if payload is not None:
                try:
                    with suppress(BrokenPipeError, ConnectionResetError):
                        process.stdin.write(payload)
                        await process.stdin.drain()
                finally:
                    process.stdin.close()
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
