"""The first tool gateway: validated file operations with durable write receipts."""

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from filelock import FileLock
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaError
from pydantic import BaseModel, ConfigDict, Field

from deep_harness.application.atomic_files import EditArgs, ReadArgs, WriteArgs, mutate, read
from deep_harness.application.capabilities import READ_ONLY_SCOPES
from deep_harness.application.permissions import approval_reason, approval_scope, sensitive_path
from deep_harness.application.shell import BashArgs, bash, shell_command
from deep_harness.application.terminal import CommandArgs, run_command, validate_command
from deep_harness.application.web_tools import (
    WEB_TOOL_ARGUMENTS,
    WEB_TOOL_DESCRIPTIONS,
    WEB_TOOL_NAMES,
    execute_web_tool,
)
from deep_harness.application.workspace import (
    MUTATING_TOOLS,
    ListFilesArgs,
    SearchFilesArgs,
    list_files,
    search_files,
)
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError, ExecutionClaim, ProfileScope, Session, ToolCall
from deep_harness.infrastructure.knowledge import KnowledgeStore


class FilePathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)


class ReadFileArgs(FilePathArgs):
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=120, ge=1, le=400)


class WriteFileArgs(FilePathArgs):
    content: str = Field(max_length=128_000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class SaveMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=1000)


class DelegateTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["research", "coding"]
    task: str = Field(min_length=1, max_length=4000)
    background: bool = False


class CheckDelegationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegation_id: str = Field(min_length=1, max_length=256)


TOOL_ARGUMENTS = {
    "read": ReadArgs,
    "write": WriteArgs,
    "edit": EditArgs,
    "bash": BashArgs,
    "list_files": ListFilesArgs,
    "search_files": SearchFilesArgs,
    "read_file": ReadFileArgs,
    "write_file": WriteFileArgs,
    "run_command": CommandArgs,
    "save_memory": SaveMemoryArgs,
    "delegate_task": DelegateTaskArgs,
    "check_delegation": CheckDelegationArgs,
}
TOOL_ARGUMENTS.update(WEB_TOOL_ARGUMENTS)
TOOL_DESCRIPTIONS = {
    "read": (
        "Read a workspace-relative UTF-8 file or list a directory. offset is a 1-based line "
        "or directory entry; limit bounds the page. Returns raw content, SHA-256 and "
        "next_offset/next_column for lossless continuation, including long lines. Max file 8 MiB."
    ),
    "write": (
        "Create or overwrite a workspace-relative UTF-8 file, creating parent directories. "
        "Overwrites require expected_sha256 from read. Atomic replacement after approval."
    ),
    "edit": (
        "Replace exactly one occurrence of old_text with new_text in a workspace UTF-8 file. "
        "Zero or multiple matches fail without changing the file. Preserve all other bytes. "
        "Optional expected_sha256 rejects stale content. Requires approval."
    ),
    "bash": (
        "Execute a shell command in workspace-relative cwd, subject to the session "
        "approval mode. Supports pipes, redirects and PATH commands. auto uses PowerShell "
        "on Windows and Bash on POSIX. Returns exit_code, stdout/stderr and truncation "
        "flags (64 KiB per stream). Timeout 1-120 seconds. Runs as the current OS user, "
        "without ambient secret env vars; the shell is not a filesystem sandbox. A timeout "
        "has an unknown effect outcome. Every command follows the session approval mode: "
        "per call in ask/legacy, once per scope per round in auto, and exempt in full."
    ),
    "list_files": (
        "List workspace files recursively with a glob pattern. Skips state, dependencies "
        "and build directories. Narrow path/pattern when truncated."
    ),
    "search_files": (
        "Search literal text in bounded UTF-8 workspace files. Returns paths, line numbers "
        "and excerpts. Narrow path/pattern when truncated."
    ),
    "read_file": (
        "Read a workspace-relative UTF-8 file by start_line/max_lines. Returns bounded content, "
        "line range, truncation flag and full-file SHA-256 (files up to 128 KB)."
    ),
    "write_file": (
        "Write a UTF-8 file after human approval. Existing files require expected_sha256 "
        "from the read result; omit it only when creating a new file."
    ),
    "run_command": (
        "Run an absolute executable with argv in the workspace after human approval. "
        "No shell expansion or ambient secrets are provided."
    ),
    "save_memory": "Save a stable profile memory for future sessions after human approval.",
    "delegate_task": (
        "Ask an isolated research or coding Deep specialist to complete a bounded task. "
        "It inherits only the corresponding granted tools; writes and commands require approval. "
        "Set background=true to launch an independent durable child session and receive a handle "
        "immediately; its work can continue after this turn. A synchronous child shares this "
        "run's budget; a background child has its own budget."
    ),
    "check_delegation": "Read the durable status and latest result of one delegated task.",
}
TOOL_DESCRIPTIONS.update(WEB_TOOL_DESCRIPTIONS)


def tool_definitions(names: tuple[str, ...], mcp_catalog=None) -> list[dict]:
    definitions = []
    for name in sorted(names):
        if name in TOOL_ARGUMENTS:
            definitions.append(
                {
                    "name": name,
                    "description": TOOL_DESCRIPTIONS[name],
                    "parameters": TOOL_ARGUMENTS[name].model_json_schema(),
                }
            )
        elif mcp_catalog is not None and name in mcp_catalog:
            spec = mcp_catalog[name]
            definitions.append(
                {
                    "name": name,
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                }
            )
        else:
            raise DomainError("unknown_tool", "Requested tool is not supported")
    return definitions


class ToolGateway:
    def __init__(
        self,
        scope: ProfileScope,
        session: Session,
        receipts,
        claim: ExecutionClaim,
        *,
        mcp=None,
        mcp_bindings=None,
        web_client=None,
    ):
        self.scope = scope
        self.session = session
        self.receipts = receipts
        self.claim = claim
        self.mcp = mcp
        self.mcp_bindings = mcp_bindings or {}
        self.web_client = web_client

    def validate(self, call: ToolCall) -> BaseModel:
        if call.name not in self.session.tools:
            raise DomainError("tool_denied", "Tool is outside the frozen session grant")
        if call.name in self.mcp_bindings:
            spec = self.mcp_bindings[call.name]
            try:
                Draft202012Validator(spec["parameters"]).validate(call.arguments)
            except JsonSchemaError as exc:
                raise DomainError(
                    "mcp_arguments", "MCP arguments do not match frozen schema"
                ) from exc
            return call.arguments
        if call.name not in TOOL_ARGUMENTS:
            raise DomainError("tool_denied", "Tool is outside the frozen session grant")
        return TOOL_ARGUMENTS[call.name].model_validate(call.arguments)

    def path(self, relative: str) -> Path:
        path = Path(relative)
        legacy = self.session.permission_mode == "legacy"
        if legacy and (path.is_absolute() or path.drive):
            raise DomainError("path_denied", "Tools accept workspace-relative paths only")
        if not legacy:
            path = path.expanduser()
        target = (self.scope.workspace / path).resolve()
        if legacy and not target.is_relative_to(self.scope.workspace):
            raise DomainError("path_denied", "Path escapes the configured workspace")
        if target.is_relative_to(self.scope.state_root):
            raise DomainError(
                "path_denied", "Runtime state and credentials are not model-accessible"
            )
        return target

    def approval_reason(self, call: ToolCall) -> str | None:
        # legacy is a compatibility row inside the shared policy table: it keeps
        # strict workspace-relative paths and must not resolve paths here.
        return approval_reason(call, self.session, self.scope, self.path)

    def approval_scope(self, call: ToolCall):
        """Return (scope_key, decision, reusable) for this call."""
        return approval_scope(call, self.session, self.scope, self.path)

    async def execute(self, call: ToolCall) -> str:
        arguments = self.validate(call)
        await self._preflight(call, arguments)
        key = f"{self.claim.admission.id}:{call.id}"
        digest = fingerprint({"name": call.name, "arguments": call.arguments})
        scope_key, _, reusable = self.approval_scope(call)
        approval = None
        audit = None
        if self.approval_reason(call) is not None:
            approval = (digest, scope_key, self.session.permission_revision, reusable)
            audit = ("direct", scope_key, call.name)
        elif self.session.permission_mode == "full" and scope_key not in READ_ONLY_SCOPES:
            audit = ("full", scope_key, call.name)
        existing = await self.receipts.prepare_effect(self.claim, key, digest)
        if existing and existing["status"] == "succeeded":
            return existing["result"]
        await self.receipts.dispatch_effect(self.claim, key, approval=approval, audit=audit)
        try:
            result = await self._invoke(call.name, arguments, key)
        except (OSError, UnicodeError, DomainError, TimeoutError) as exc:
            refused = isinstance(exc, DomainError) and exc.code in {
                "file_conflict",
                "edit_match",
                "not_file",
                "file_too_large",
                "binary_file",
                "text_encoding",
                "path_denied",
                "command_cwd",
                "shell_unavailable",
                "command_argument",
                "web_url",
                "web_private_url",
                "web_disabled",
                "web_unavailable",
                "web_timeout",
                "web_backend",
                "web_error",
            }
            if not refused and (call.name in MUTATING_TOOLS or (call.name in self.mcp_bindings)):
                if isinstance(exc, TimeoutError) and isinstance(arguments, (BashArgs, CommandArgs)):
                    detail = f"工具 {call.name} 执行超时（{arguments.timeout_seconds:g} 秒）"
                else:
                    detail = f"工具 {call.name} 派发后失败（{type(exc).__name__}）"
                raise DomainError(
                    "unknown_effect",
                    f"{detail}；执行结果未确认，请核对实际结果后再继续，不会自动重试。",
                ) from exc
            code = exc.code if isinstance(exc, DomainError) else type(exc).__name__
            result = f"Tool error [{code}]: {exc}"
        await self.receipts.save_effect(self.claim, key, result)
        return result

    async def _preflight(self, call, arguments):
        if call.name in {"delegate_task", "check_delegation"}:
            raise DomainError("tool_denied", "Delegation is managed by the Deep runtime")
        if call.name in WEB_TOOL_NAMES and self.web_client is None:
            raise DomainError("web_disabled", "Web retrieval client is not configured")
        if call.name in self.mcp_bindings:
            if self.mcp is None:
                raise DomainError("mcp_unavailable", "MCP registry is not configured")
        elif call.name == "bash":
            await asyncio.to_thread(shell_command, arguments, self.path)
        elif call.name == "run_command":
            await asyncio.to_thread(validate_command, arguments, self.path)
        elif call.name in {
            "read",
            "write",
            "edit",
            "read_file",
            "write_file",
            "list_files",
            "search_files",
        }:
            await asyncio.to_thread(self.path, arguments.path)

    async def _invoke(self, name, arguments, key):
        if name in self.mcp_bindings:
            return await self.mcp.call(name, arguments, self.mcp_bindings[name]["schema_hash"])
        if name in WEB_TOOL_NAMES:
            return await execute_web_tool(name, arguments, self.web_client)
        commands = {"bash": bash, "run_command": run_command}
        if name in commands:
            return json.dumps(await commands[name](arguments, self.path), ensure_ascii=False)
        if name == "save_memory":
            memory = await KnowledgeStore(self.receipts.store).remember(
                arguments.content, "agent", memory_id=key
            )
            return json.dumps({"memory_id": memory["id"]})
        if name in {"read", "list_files", "search_files"}:
            handler = {"read": read, "list_files": list_files, "search_files": search_files}[name]
            root = self.path(arguments.path)

            def candidate_path(path):
                target = self.path(path)
                if self.session.permission_mode in {"ask", "auto"} and (
                    not target.is_relative_to(root)
                    or (sensitive_path(target) and not sensitive_path(root))
                ):
                    raise DomainError("path_denied", "Nested path is outside the approved scope")
                return target

            value = await asyncio.to_thread(handler, arguments, self.path, candidate_path)
            return json.dumps(value, ensure_ascii=False)
        handlers = {"write": self._mutate, "edit": self._mutate}
        if name in handlers:
            value = await asyncio.to_thread(handlers[name], arguments, self.path)
            return json.dumps(value, ensure_ascii=False)
        handler = {"read_file": self._read, "write_file": self._write}[name]
        return await asyncio.to_thread(handler, arguments)

    def _mutate(self, arguments, workspace_path):
        # Serialize read/check/replace across profiles and workers using the same state root.
        target = workspace_path(arguments.path)
        key = hashlib.sha256(os.path.normcase(str(target)).encode()).hexdigest()
        locks = self.scope.state_root / "file-locks"
        locks.mkdir(parents=True, exist_ok=True)
        with FileLock(locks / f"{key}.lock", timeout=10):
            return mutate(arguments, workspace_path)

    def _read(self, arguments: ReadFileArgs) -> str:
        target = self.path(arguments.path)
        with target.open("rb") as stream:
            data = stream.read(128_001)
        if len(data) > 128_000:
            raise DomainError("file_too_large", "File exceeds this prototype's 128 KB limit")
        lines = data.decode("utf-8").splitlines(keepends=True)
        selected = lines[arguments.start_line - 1 : arguments.start_line - 1 + arguments.max_lines]
        content = ""
        for line in selected:
            if len(content.encode("utf-8")) + len(line.encode("utf-8")) > 6000:
                break
            content += line
        # A single long line still needs to be inspectable.
        if not content and selected:
            content = selected[0].encode("utf-8")[:6000].decode("utf-8", errors="ignore")
        end_line = arguments.start_line - 1 + len(content.splitlines())
        return json.dumps(
            {
                "path": arguments.path,
                "content": content,
                "start_line": arguments.start_line,
                "end_line": end_line,
                "total_lines": len(lines),
                "truncated": end_line < len(lines) or content != "".join(selected),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
            ensure_ascii=False,
        )

    def _write(self, arguments: WriteFileArgs) -> str:
        target = self.path(arguments.path)
        if target.exists():
            current = hashlib.sha256(target.read_bytes()).hexdigest()
            if arguments.expected_sha256 != current:
                raise DomainError(
                    "file_conflict",
                    "Existing file changed or expected hash is missing",
                )
        elif arguments.expected_sha256 is not None:
            raise DomainError("file_conflict", "Expected existing file is missing")
        data = arguments.content.encode("utf-8")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return json.dumps({"path": arguments.path, "sha256": hashlib.sha256(data).hexdigest()})
