"""Conservative approval policy, independent of model instructions."""

import re

from deep_harness.application.shell import shell_kind
from deep_harness.application.workspace import MUTATING_TOOLS

SENSITIVE_PARTS = {".git", ".ssh", ".aws", ".azure", ".kube", ".gnupg"}
SENSITIVE_NAMES = {"agents.md", "claude.md", ".env", ".npmrc", ".netrc", "credentials"}
FILE_TOOLS = {"read", "write", "edit", "read_file", "write_file", "list_files", "search_files"}
FILE_WRITES = {"write", "edit", "write_file"}


def sensitive_path(target):
    names = [part.lower() for part in target.parts]
    return bool(set(names) & (SENSITIVE_PARTS | SENSITIVE_NAMES)) or any(
        name.startswith(".env.") or name.endswith((".pem", ".key")) for name in names
    )


def approval_reason(call, session, scope, resolve_path):
    mode = session.permission_mode
    if mode == "full":
        return None
    if call.name in FILE_TOOLS:
        target = resolve_path(call.arguments.get("path", "."))
        if not target.is_relative_to(scope.workspace):
            return "访问工作区外的文件"
        if sensitive_path(target):
            return "访问凭据、版本控制或 Agent 指令文件"
        if call.name in FILE_WRITES:
            return None if mode == "auto" else "修改文件需要批准"
        return None
    if call.name in {"bash", "run_command"}:
        cwd = resolve_path(call.arguments.get("cwd", "."))
        if not cwd.is_relative_to(scope.workspace) or sensitive_path(cwd):
            return "在工作区外或敏感目录执行命令"
        # Only literal shell builtins are auto-approved. Network, substitutions,
        # pipelines and repository hooks cannot be inferred safe from a command name.
        command = call.arguments.get("command", "").strip()
        if mode == "auto" and call.name == "bash" and re.fullmatch(r"[\w .,:/+\-]+", command):
            words = command.split()
            kind = shell_kind(call.arguments.get("shell", "auto"))
            builtin = words[0] if kind == "bash" else words[0].lower()
            allowed = (
                {"pwd", "echo"}
                if kind == "bash"
                else {"pwd", "echo", "get-location", "write-output"}
            )
            if builtin in allowed:
                return None
        return "命令可能访问网络、修改文件或产生其他副作用"
    if call.name.startswith("mcp__"):
        return "外部服务可能访问网络或产生副作用"
    if call.name in MUTATING_TOOLS:
        return "修改持久状态需要批准"
    return None
