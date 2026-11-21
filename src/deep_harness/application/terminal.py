"""Approval-gated native command runner with bounded output and no shell expansion."""

import asyncio
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from deep_harness.application.process_environment import command_environment
from deep_harness.domain.models import DomainError
from deep_harness.server_config import number


class CommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str] = Field(min_length=1, max_length=number("native_command.argv_max_length"))
    cwd: str = Field(default=".", min_length=1, max_length=number("native_command.cwd_max_length"))
    timeout_seconds: float = Field(
        default=number("native_command.timeout_seconds"),
        ge=1,
        le=number("native_command.timeout_seconds_max"),
    )


async def _read_bounded(stream, limit):
    chunks = []
    size = 0
    while block := await stream.read(number("streams.read_chunk_bytes")):
        size += len(block)
        if size > limit:
            raise DomainError(
                "command_output_limit", "Command output exceeded the configured per-stream limit"
            )
        chunks.append(block)
    return b"".join(chunks)


def validate_command(arguments: CommandArgs, workspace_path):
    executable = Path(arguments.argv[0])
    if not executable.is_absolute() or not executable.is_file():
        raise DomainError(
            "command_executable", "Command executable must be an existing absolute path"
        )
    cwd = workspace_path(arguments.cwd)
    if not cwd.is_dir():
        raise DomainError("command_cwd", "Command working directory does not exist")
    if any(
        "\x00" in argument or len(argument) > number("native_command.argument_max_chars")
        for argument in arguments.argv
    ):
        raise DomainError("command_argument", "Command contains an invalid or oversized argument")
    return cwd


async def run_command(arguments: CommandArgs, workspace_path):
    cwd = validate_command(arguments, workspace_path)
    environment = command_environment()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = await asyncio.create_subprocess_exec(
        *arguments.argv,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
    )
    try:
        async with asyncio.timeout(arguments.timeout_seconds):
            stdout, stderr = await asyncio.wait_for(
                asyncio.gather(
                    _read_bounded(process.stdout, number("shell.stream_limit_bytes")),
                    _read_bounded(process.stderr, number("shell.stream_limit_bytes")),
                ),
                timeout=arguments.timeout_seconds,
            )
            code = await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(
                process.wait(), timeout=number("native_command.terminate_wait_seconds")
            )
    return {
        "exit_code": code,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
