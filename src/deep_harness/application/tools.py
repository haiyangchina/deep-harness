"""The first tool gateway: validated file operations with durable write receipts."""

import asyncio
import hashlib
import json
import logging
import os
import re
from contextlib import AsyncExitStack, nullcontext
from dataclasses import asdict
from functools import lru_cache, partial
from pathlib import Path
from typing import Literal
from weakref import WeakKeyDictionary

from filelock import AsyncFileLock, FileLock
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaError
from pydantic import BaseModel, ConfigDict, Field

from deep_harness.application.atomic_files import (
    EditArgs,
    ReadArgs,
    WriteArgs,
    atomic_write,
    mutate,
    mutation_data,
)
from deep_harness.application.capabilities import READ_ONLY_SCOPES
from deep_harness.application.patches import PatchArgs, prepare_file
from deep_harness.application.permissions import approval_reason, approval_scope, sensitive_path
from deep_harness.application.shell import BashArgs, bash, shell_command
from deep_harness.application.symbols import NavigationArgs, SymbolArgs, symbols
from deep_harness.application.web_tools import (
    WEB_TOOL_ARGUMENTS,
    WEB_TOOL_DESCRIPTIONS,
    WEB_TOOL_NAMES,
    execute_web_tool,
)
from deep_harness.application.workspace import (
    MUTATING_TOOLS,
    DirectoryListArgs,
    ProjectInstructionsArgs,
    ReadFilesArgs,
    SearchFilesArgs,
    list_files,
    project_instructions,
    read_file_evidence,
)
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError, ExecutionClaim, ProfileScope, Session, ToolCall
from deep_harness.infrastructure.knowledge import KnowledgeStore
from deep_harness.infrastructure.lsp import navigate
from deep_harness.infrastructure.task_workspaces import (
    finish_io,
    run_workspace_process,
    workspace_lease,
)
from deep_harness.server_config import number

logger = logging.getLogger(__name__)
PROJECT_WRITERS = frozenset({"write", "edit", "apply_patch", "bash"})
LOCAL_READS = frozenset({
    "read", "list_files", "read_files", "search_files", "symbols", "project_instructions",
    "code_navigation",
})
_READ_SLOTS = WeakKeyDictionary()
_DIRECTORY_SLOTS = WeakKeyDictionary()
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)(^\s*(?:export\s+)?[\w.-]*(?:key|token|secret|password|credential)[\w.-]*"
    r"\s*[=:]\s*)[^\r\n]+"
)
_PRIVATE_KEY = re.compile(r"(?s)-----BEGIN [^\n]*PRIVATE KEY-----.*?(?:-----END [^\n]+-----|\Z)")


def redact_command_output(text):
    return _SECRET_ASSIGNMENT.sub(r"\1[redacted]", _PRIVATE_KEY.sub("[redacted]", text))


def redact_shell_receipt(content):
    if not isinstance(content, str):
        return content
    try:
        data = json.loads(content)
    except ValueError:
        return redact_command_output(content)
    if not isinstance(data, dict):
        return redact_command_output(content)
    modified = False
    for channel in ("stdout", "stderr"):
        if isinstance(data.get(channel), str):
            safe = redact_command_output(data[channel])
            if safe != data[channel]:
                modified = True
                data[channel] = safe
    return json.dumps(data, ensure_ascii=False) if modified else content


async def local_io(function, *args):
    loop = asyncio.get_running_loop()
    slots = _READ_SLOTS.setdefault(loop, asyncio.Semaphore(4))
    await slots.acquire()
    try:
        operation = loop.run_in_executor(None, function, *args)
    except BaseException:
        slots.release()
        raise

    def settled(done):
        slots.release()
        if not done.cancelled():
            done.exception()

    operation.add_done_callback(settled)
    return await asyncio.shield(operation)


class SaveMemoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=number("memory.content_max_length"))


class DelegateTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["research", "coding", "review"]
    task: str = Field(min_length=1, max_length=number("delegation.task_max_length"))
    background: bool = False
    share_images: bool | None = Field(
        default=None, description="Pass the current user message's images to this specialist"
    )
    isolate: bool | None = Field(
        default=None, description="For synchronous implementation, use a separate task copy"
    )


class CheckDelegationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegation_id: str = Field(min_length=1, max_length=number("api.delegation_id_max_length"))


class MergeDelegationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegation_ids: list[str] = Field(min_length=1, max_length=4)


TOOL_ARGUMENTS = {
    "read": ReadArgs,
    "write": WriteArgs,
    "edit": EditArgs,
    "apply_patch": PatchArgs,
    "bash": BashArgs,
    "list_files": DirectoryListArgs,
    "read_files": ReadFilesArgs,
    "search_files": SearchFilesArgs,
    "project_instructions": ProjectInstructionsArgs,
    "symbols": SymbolArgs,
    "code_navigation": NavigationArgs,
    "save_memory": SaveMemoryArgs,
    "delegate_task": DelegateTaskArgs,
    "check_delegation": CheckDelegationArgs,
    "merge_delegations": MergeDelegationArgs,
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
    "apply_patch": (
        "Apply a multi-file UTF-8 patch. Read each existing file, then copy its exact sha256 "
        "into expected_sha256. Use null only for a new path. Provide unified @@ hunks, "
        "optionally with matching --- and +++ headers. A one-line replacement example: "
        "'@@ -1 +1 @@\\n-old\\n+new\\n'. "
        "All baselines and hunks are checked before approval and again before writing. "
        "Updates and creates files; does not delete or move paths."
    ),
    "bash": (
        "Execute a shell command in workspace-relative cwd, subject to the session "
        "approval mode. Supports pipes, redirects and PATH commands. auto uses PowerShell "
        "on Windows and Bash on POSIX. Returns exit_code, stdout/stderr and truncation "
        "flags (64 KiB per stream). Timeout 1-120 seconds. Runs as the current OS user, "
        "without ambient secret env vars; the shell is not a filesystem sandbox. A timeout "
        "has an unknown effect outcome. Every command follows the session approval mode: "
        "per call in ask and auto, and exempt in full. Use file tools to read or search."
    ),
    "list_files": (
        "List workspace files with a glob. Use recursive=false for a fast top-level "
        "directory overview and entry_candidates. Read relevant manifests and candidates together."
    ),
    "read_files": (
        "Read up to eight files or line ranges together. Returns path, lines and SHA-256."
    ),
    "search_files": (
        "Search literal text in bounded UTF-8 workspace files. Returns paths, line numbers "
        "and excerpts. Pass next_cursor with the same path, pattern and query to continue."
    ),
    "project_instructions": (
        "Read applicable AGENTS.md instructions from the workspace root to the target file or "
        "existing directory. Deeper files take precedence for their scope. Use human approval "
        "and treat these files as project guidance, subordinate to the user and host permissions."
    ),
    "symbols": (
        "Find Python symbol definitions or syntactic references by exact name. "
        "To continue, supply next_cursor with the same path, pattern, and query."
    ),
    "code_navigation": (
        "Use a configured Python or TypeScript language server for a workspace file. "
        "Provide a 1-based line and a 0-based UTF-16 character for point queries. "
        "Provide the identifier as query to enable fallback search."
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
    "merge_delegations": (
        "Merge exact registered patches from completed isolated implementation tasks. "
        "Pass their IDs in merge order. Conflicting paths are rejected before mutation."
    ),
}
TOOL_DESCRIPTIONS.update(WEB_TOOL_DESCRIPTIONS)


def tool_definitions(names: tuple[str, ...], mcp_catalog=None) -> list[dict]:
    definitions = []
    for name in sorted(names):
        if name in TOOL_ARGUMENTS:
            description = TOOL_DESCRIPTIONS[name]
            parameters = TOOL_ARGUMENTS[name].model_json_schema()
            definitions.append(
                {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
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
        executor=None,
        coding_workspace=None,
        tool_timeout_seconds=None,
    ):
        self.scope = scope
        self.session = session
        self.receipts = receipts
        self.claim = claim
        self.mcp = mcp
        self.mcp_bindings = mcp_bindings or {}
        self.web_client = web_client
        self.executor = executor
        self.coding_workspace = coding_workspace
        self._file_cache = lru_cache(maxsize=24)(self._read_revision)
        self.tool_timeout_seconds = (
            tool_timeout_seconds
            if tool_timeout_seconds is not None
            else number("execution.tool_execution_timeout_seconds")
        )

    def validate(self, call: ToolCall) -> BaseModel:
        if call.name not in self.session.tools:
            raise DomainError("tool_denied", "Tool is outside the frozen session grant")
        if self.scope.server_mode:
            unavailable = {"save_memory"}
            if not self.scope.task_workspace:
                unavailable.update({"write", "edit", "apply_patch", "delegate_task"})
            if call.name in unavailable or (
                call.name == "bash" and (self.scope.server_mode != "sandbox" or not self.executor)
            ):
                raise DomainError("tool_denied", "This tool is unavailable on the server")
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
        path = Path(relative).expanduser()
        target = (self.scope.workspace / path).resolve()
        inside = target.is_relative_to(self.scope.workspace)
        in_task = self.scope.task_workspace and inside
        if target.is_relative_to(self.scope.state_root) and not in_task:
            raise DomainError(
                "path_denied", "Runtime state and credentials are not model-accessible"
            )
        if (self.scope.server_mode or self.scope.task_workspace) and (
            not inside
            or any(target.is_relative_to(root) for root in self.scope.protected_paths)
        ):
            raise DomainError("path_denied", "Path is outside the available server workspace")
        return target

    def approval_reason(self, call: ToolCall) -> str | None:
        return approval_reason(call, self.session, self.scope, self.path)

    def approval_scope(self, call: ToolCall):
        """Return (scope_key, decision, reusable) for this call."""
        return approval_scope(call, self.session, self.scope, self.path)

    async def check_repeated_command(self, call: ToolCall):
        if call.name != "bash":
            return
        prior = await self.receipts.latest_non_read_tool_effect(self.claim, LOCAL_READS)
        digest = fingerprint({"name": call.name, "arguments": call.arguments})
        if (
            prior is None
            or prior["id"] == f"{self.claim.admission.id}:{call.id}"
            or prior["proposal_hash"] != digest
            or prior["status"] != "succeeded"
            or not prior["result"]
            or not prior["result"].startswith("{")
        ):
            return
        exit_code = json.loads(prior["result"]).get("exit_code")
        if exit_code:
            raise DomainError(
                "command_already_failed",
                f"同一命令已以退出码 {exit_code} 失败，请根据错误调整命令后再试；"
                "若只需检索项目，使用 search_files 和 read_files。",
            )

    async def interaction(self, call, key, digest, proposal):
        before_open = None
        if call.name == "apply_patch":

            async def before_open():
                arguments = self.validate(call)
                await asyncio.to_thread(self._patch_plan, arguments)

        return await self.receipts.interaction(
            self.claim, key, digest, proposal, before_open=before_open
        )

    async def execute(self, call: ToolCall) -> str:
        arguments = self.validate(call)
        if call.name not in LOCAL_READS:
            await self._preflight(call, arguments)
        key = f"{self.claim.admission.id}:{call.id}"
        project_write = call.name in PROJECT_WRITERS
        if project_write:
            await self._progress("workspace.waiting", key, call.name, phase="waiting")
        async with workspace_lease(self.scope.workspace) if project_write else nullcontext():
            return await self._execute(call, arguments, key)

    async def _execute(self, call, arguments, key):
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
            result = existing["result"]
            return redact_shell_receipt(result) if call.name == "bash" else result
        if call.name in {"write", "edit"}:
            await asyncio.to_thread(mutation_data, arguments, self.path(arguments.path))
        dispatched = False
        clock = asyncio.get_running_loop().time
        execution_started = clock()
        failure = None
        shallow_fallback = False

        async def dispatch():
            nonlocal dispatched
            await self.receipts.dispatch_effect(self.claim, key, approval=approval, audit=audit)
            dispatched = True
            # Emit a progress event at the moment the tool starts doing real work.
            # Without this the activity feed stays silent for the whole (possibly
            # very long) tool await and the UI appears frozen.
            await self._progress("tool.started", key, call.name, phase="started")

        verify = (
            self.coding_workspace is not None
            and call.name == "bash"
            and arguments.purpose == "verify"
            and self.path(arguments.cwd).is_relative_to(self.scope.workspace)
        )
        if self.coding_workspace is not None and call.name in PROJECT_WRITERS:
            await finish_io(self.coding_workspace.begin)
        before = await asyncio.to_thread(self.coding_workspace.revision) if verify else None
        if call.name not in self.mcp_bindings and call.name != "apply_patch":
            await dispatch()
        try:
            # Bound a single tool await. Otherwise one hung I/O call consumes the
            # entire wall budget (600s) while emitting no progress and no error.
            # A TimeoutError here is handled below with the existing durable
            # semantics: mutating/MCP effects become unknown_effect, read-only
            # tools return a normal tool error (no side effects to reconcile).
            timeout = self.tool_timeout_seconds
            if call.name in LOCAL_READS:
                quick = call.name in {"read", "read_files", "project_instructions"} or (
                    call.name == "list_files" and not arguments.recursive
                )
                timeout = min(timeout, 3 if quick else 8)
            async with asyncio.timeout(timeout):
                if call.name == "apply_patch":
                    result = await self._apply_patch(arguments, key, dispatch)
                elif call.name in self.mcp_bindings:
                    result = await self.mcp.call(
                        call.name,
                        arguments,
                        self.mcp_bindings[call.name]["schema_hash"],
                        before_dispatch=dispatch,
                        effect_id=key,
                    )
                else:
                    result = await self._invoke(call.name, arguments, key)
        except (OSError, UnicodeError, DomainError, TimeoutError) as exc:
            if not dispatched:
                if isinstance(exc, DomainError):
                    raise
                category = "patch_preflight" if call.name == "apply_patch" else "mcp_unavailable"
                raise DomainError(category, "Tool preparation failed before dispatch") from exc
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
            if call.name == "apply_patch" or (
                not refused and (call.name in MUTATING_TOOLS or call.name in self.mcp_bindings)
            ):
                if isinstance(exc, TimeoutError) and isinstance(arguments, BashArgs):
                    detail = f"工具 {call.name} 执行超时（{arguments.timeout_seconds:g} 秒）"
                else:
                    detail = f"工具 {call.name} 派发后失败（{type(exc).__name__}）"
                raise DomainError(
                    "unknown_effect",
                    f"{detail}；执行结果未确认，请核对实际结果后再继续，不会自动重试。",
                ) from exc
            code = exc.code if isinstance(exc, DomainError) else type(exc).__name__
            failure = code
            result = f"Tool error [{code}]: {exc}"
            if isinstance(exc, TimeoutError) and call.name in {"list_files", "search_files"}:
                result += "Use list_files with recursive=false or read a narrower directory."
            if isinstance(exc, TimeoutError) and call.name == "list_files" and arguments.recursive:
                fallback = arguments.model_copy(update={"recursive": False})
                try:
                    async with asyncio.timeout(min(self.tool_timeout_seconds, 3)):
                        data = json.loads(await self._invoke(call.name, fallback, key))
                        data.update(truncated=True, fallback_reason="recursive_timeout")
                        result = json.dumps(data, ensure_ascii=False)
                        shallow_fallback = True
                except (TimeoutError, OSError, DomainError):
                    pass
        except Exception as exc:
            if call.name not in self.mcp_bindings:
                raise
            if not dispatched:
                raise DomainError("mcp_unavailable", "MCP tool preparation failed") from exc
            raise DomainError("unknown_effect", "MCP tool outcome must be reconciled") from exc
        if call.name == "bash" and result.startswith("{") and json.loads(result).get("exit_code"):
            failure = "command_exit"
        verification = None
        if verify and result.startswith("{"):
            verification = {
                "command": arguments.command,
                "cwd": arguments.cwd,
                "exit_code": json.loads(result)["exit_code"],
                "before_revision": before,
                "after_revision": await asyncio.to_thread(self.coding_workspace.revision),
            }
        await self.receipts.save_effect(self.claim, key, result, verification=verification)
        await self._progress(
            "tool.failed" if failure else "tool.completed",
            key,
            call.name,
            phase="failed" if failure else "completed",
            **({"error_code": failure} if failure else {}),
            **({"fallback": "shallow"} if shallow_fallback else {}),
            elapsed_ms=round(
                (asyncio.get_running_loop().time() - execution_started) * 1000
            ),
        )
        return result

    async def _progress(self, kind: str, effect_id: str, tool: str, **payload):
        """Best-effort progress event. Never let telemetry break tool execution."""
        try:
            async with self.receipts.store.transaction() as conn:
                await self.receipts.executions.assert_claim(conn, self.claim)
                await self.receipts.store.append_event(
                    conn,
                    self.session.id,
                    kind,
                    {
                        "admission_id": self.claim.admission.id,
                        "effect_id": effect_id, "tool": tool, **payload,
                    },
                )
        except Exception:
            # Progress reporting must not affect the effect's correctness.
            logger.warning("Failed to emit tool progress event %s", kind, exc_info=True)

    async def _preflight(self, call, arguments):
        if call.name in {"delegate_task", "check_delegation"}:
            raise DomainError("tool_denied", "Delegation is managed by the Deep runtime")
        if call.name in WEB_TOOL_NAMES and self.web_client is None:
            raise DomainError("web_disabled", "Web retrieval client is not configured")
        if call.name in self.mcp_bindings:
            if self.mcp is None:
                raise DomainError("mcp_unavailable", "MCP registry is not configured")
        elif call.name == "bash":
            if self.executor is not None:
                self.executor.validate(arguments, self.path)
                if not self.scope.task_workspace:
                    run_id = self.claim.admission.id
                    await self.executor.prepare(
                        run_id, self.scope.workspace,
                        previously_used=await self.receipts.used_sandbox(run_id),
                    )
            else:
                await asyncio.to_thread(shell_command, arguments, self.path)
        elif call.name in {
            "read",
            "write",
            "edit",
            "list_files",
            "search_files",
        }:
            await asyncio.to_thread(self.path, arguments.path)

    async def _invoke(self, name, arguments, key):
        if name in WEB_TOOL_NAMES:
            return await execute_web_tool(name, arguments, self.web_client)
        if name == "bash":
            if self.executor is not None and self.scope.task_workspace:
                result = await self.executor.run_managed(arguments, self.path, self.scope.workspace)
            elif self.executor is not None:
                result = await self.executor.run(
                    arguments, self.path, self.scope.workspace, run_id=self.claim.admission.id
                )
            elif self.coding_workspace is not None and self.scope.task_workspace:
                result = await bash(arguments, self.path, git_ceiling=self.scope.workspace.parent)
            else:
                result = await bash(arguments, self.path)
            for channel in ("stdout", "stderr"):
                if isinstance(result.get(channel), str):
                    result[channel] = redact_command_output(result[channel])
                    if self.coding_workspace is not None and result[channel]:
                        async with self.receipts.store.transaction() as conn:
                            await self.receipts.executions.assert_claim(conn, self.claim)
                            await self.receipts.store.append_event(
                                conn, self.session.id, "coding.command_output",
                                {"id": key, "channel": channel, "content": result[channel]},
                            )
            return json.dumps(result, ensure_ascii=False)
        if name == "save_memory":
            memory = await KnowledgeStore(self.receipts.store).remember(
                arguments.content, "agent", memory_id=key
            )
            return json.dumps({"memory_id": memory["id"]})
        if name == "project_instructions":
            value = await local_io(
                project_instructions, arguments, self.scope.workspace, self.path
            )
            return json.dumps(value, ensure_ascii=False)
        if name == "code_navigation":
            return json.dumps(await self.navigation(arguments), ensure_ascii=False)
        if name == "read_files":
            async def one(item):
                try:
                    async with asyncio.timeout(min(self.tool_timeout_seconds * 0.85, 2.5)):
                        item = item.model_copy(update={"limit": min(item.limit, 50)})
                        reader = partial(self._read_cached, output_chars=2400)
                        result = await local_io(reader, item)
                    return {"path": item.path, **result}
                except (DomainError, OSError, UnicodeError, TimeoutError) as exc:
                    return {
                        "path": item.path,
                        "error": exc.code if isinstance(exc, DomainError) else type(exc).__name__,
                    }

            results = await asyncio.gather(*(one(item) for item in arguments.files))
            return json.dumps({"files": results}, ensure_ascii=False)
        if name == "search_files" or (name == "list_files" and arguments.recursive):
            loop = asyncio.get_running_loop()
            slots = _DIRECTORY_SLOTS.setdefault(loop, asyncio.Semaphore(2))
            queued = loop.time()
            async with slots:
                started = loop.time()
                payload = json.dumps({
                    "name": name, "arguments": arguments.model_dump(),
                    "scope": asdict(self.scope), "permission_mode": self.session.permission_mode,
                }, default=str).encode("utf-8")
                output = await run_workspace_process("workspace_reader", payload)
            if "error" in output:
                raise DomainError(output["error"], output["message"])
            result = output["value"]
            result.setdefault("timings", {}).update(
                queue_ms=round((started - queued) * 1000),
                worker_ms=round((loop.time() - started) * 1000),
            )
            return json.dumps(result, ensure_ascii=False)
        if name in {"read", "list_files", "symbols"}:
            handler = {
                "read": read_file_evidence, "list_files": list_files,
                "symbols": symbols,
            }[name]
            value = (
                await local_io(self._read_cached, arguments)
                if name == "read" else await local_io(self._read_local, handler, arguments)
            )
            return json.dumps(value, ensure_ascii=False)
        handlers = {"write": self._mutate, "edit": self._mutate}
        if name in handlers:
            value = await finish_io(handlers[name], arguments, self.path)
            return json.dumps(value, ensure_ascii=False)
        raise DomainError("unknown_tool", "Requested tool is not supported")

    def _read_cached(self, arguments, *, output_chars=None):
        target = self.path(arguments.path)
        if not target.is_file() or sensitive_path(target):
            options = {} if output_chars is None else {"output_chars": output_chars}
            return read_file_evidence(
                arguments, self.path, self._candidate_for(arguments.path), **options,
            )
        stat = target.stat()
        revision = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
        return self._file_cache(arguments.model_dump_json(), target, revision, output_chars)

    def _read_revision(self, arguments_json, target, revision, output_chars):
        arguments = ReadArgs.model_validate_json(arguments_json)
        options = {} if output_chars is None else {"output_chars": output_chars}

        def resolved(path):
            return target if path == arguments.path else self.path(path)

        result = read_file_evidence(arguments, resolved, **options)
        stat = target.stat()
        current = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
        if current != revision:
            raise DomainError("file_conflict", "File changed during the read")
        return result

    def _read_local(self, handler, arguments):
        shallow = isinstance(arguments, DirectoryListArgs) and not arguments.recursive
        return handler(arguments, self.path, self._candidate_for(arguments.path, shallow=shallow))

    def _candidate_for(self, path, *, shallow=False):
        root = self.path(path)

        def candidate(name):
            target = self.path(name)
            if (shallow or self.session.permission_mode in {"ask", "auto"}) and (
                not target.is_relative_to(root)
                or (sensitive_path(target) and not sensitive_path(root))
            ):
                raise DomainError("path_denied", "Nested path is outside the approved scope")
            return target

        return candidate

    async def navigation(self, arguments):
        root = self.scope.workspace
        current = self.path(arguments.path)
        if not current.is_relative_to(root) or sensitive_path(current) or not current.is_file():
            raise DomainError("path_denied", "Navigation requires an ordinary workspace file")

        def candidate(name):
            path = self.path(name)
            if not path.is_relative_to(root) or sensitive_path(path):
                raise DomainError("path_denied", "Navigation result is outside the workspace")
            return path

        if not self.scope.server_mode:
            revision = await asyncio.to_thread(self.coding_workspace.revision) if (
                self.coding_workspace is not None
            ) else None
            result = await navigate(root, current, arguments, candidate)
            if result is not None:
                current_revision = await asyncio.to_thread(self.coding_workspace.revision) if (
                    revision is not None
                ) else None
                if revision == current_revision:
                    return {"source": "lsp", "revision": revision, "matches": result}
        if arguments.query and arguments.operation != "diagnostics":
            if current.suffix.lower() in {".py", ".pyi"}:
                result = await asyncio.to_thread(
                    symbols, SymbolArgs(query=arguments.query), self.path, candidate
                )
                return {"source": "syntax", **result}
            result = await asyncio.to_thread(
                            SearchFilesArgs(query=arguments.query, pattern="*" + current.suffix),
                self.path, candidate,
            )
            return {"source": "text", **result}
        return {"source": "unavailable", "matches": []}

    def _patch_targets(self, arguments):
        targets = {}
        for patch in arguments.files:
            path = Path(patch.path)
            target = self.path(patch.path)
            if (
                path.anchor
                or ".." in path.parts
                or not target.is_relative_to(self.scope.workspace)
                or os.path.normcase(str(self.scope.workspace / path))
                != os.path.normcase(str(target))
                or sensitive_path(target)
            ):
                raise DomainError("path_denied", "Patch requires ordinary workspace-relative paths")
            if target in targets:
                raise DomainError("patch_format", "Each patch target must be unique")
            targets[target] = patch
        return targets

    def _patch_plan(self, arguments):
        return {
            target: prepare_file(target, patch)
            for target, patch in self._patch_targets(arguments).items()
        }

    async def _patch_event(self, key, event, **payload):
        async with self.receipts.store.transaction() as conn:
            await self.receipts.executions.assert_claim(conn, self.claim)
            await self.receipts.store.append_event(
                conn, self.session.id, event, {"effect_id": key, **payload}
            )

    async def _apply_patch(self, arguments, key, dispatch):
        targets = self._patch_targets(arguments)
        async with AsyncExitStack() as locks:
            for target in sorted(targets):
                await locks.enter_async_context(
                    AsyncFileLock(
                        self._lock_path(target), timeout=number("files.write_lock_seconds")
                    )
                )
            plan = await asyncio.to_thread(self._patch_plan, arguments)
            if plan.keys() != targets.keys():
                raise DomainError("file_conflict", "Patch targets changed during preflight")
            files = [
                {
                    "path": patch.path,
                    "expected_sha256": patch.expected_sha256,
                    "sha256": hashlib.sha256(plan[target]).hexdigest()
                    if plan[target] is not None else None,
                }
                for target, patch in targets.items()
            ]
            await dispatch()
            await self._patch_event(key, "patch.planned", files=files)
            for (target, patch), evidence in zip(targets.items(), files, strict=True):
                await self._patch_event(key, "patch.file_dispatched", **evidence)
                await finish_io(self._write_patch_file, target, patch)
                await self._patch_event(key, "patch.file_written", **evidence)
            return json.dumps({"files": files}, ensure_ascii=False)

    def _write_patch_file(self, target, patch):
        if self.path(patch.path) != target:
            raise DomainError("file_conflict", "Patch target moved after approval")
        data = prepare_file(target, patch)
        if data is None:
            target.unlink()
        else:
            atomic_write(target, data)

    def _lock_path(self, target):
        key = hashlib.sha256(os.path.normcase(str(target)).encode()).hexdigest()
        locks = self.scope.state_root / "file-locks"
        locks.mkdir(parents=True, exist_ok=True)
        return locks / f"{key}.lock"

    def _mutate(self, arguments, workspace_path):
        target = workspace_path(arguments.path)
        with FileLock(self._lock_path(target), timeout=number("files.write_lock_seconds")):
            return mutate(arguments, workspace_path)
