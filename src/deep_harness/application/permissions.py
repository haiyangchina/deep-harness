"""Conservative approval policy, independent of model instructions.

One policy table decides every tool call. ``classify`` maps a call to a risk
scope without inspecting command text; ``approval_reason`` then applies the
session mode. The same table is published through the capability manifest.
"""

from deep_harness.application.capabilities import (
    SCOPE_FILE_READ,
    SCOPE_FILE_WRITE,
    SCOPE_INTERNET,
    SCOPE_OUTSIDE,
    SCOPE_PERSISTENT,
    SCOPE_SENSITIVE,
    SCOPE_TERMINAL,
    WEB_TOOLS,
    policy_for,
    reusable,
)
from deep_harness.application.workspace import MUTATING_TOOLS

SENSITIVE_PARTS = {".git", ".ssh", ".aws", ".azure", ".kube", ".gnupg"}
SENSITIVE_NAMES = {"agents.md", "claude.md", ".env", ".npmrc", ".netrc", "credentials"}
FILE_TOOLS = {"read", "write", "edit", "list_files", "search_files"}
FILE_WRITES = {"write", "edit"}


def sensitive_path(target):
    names = [part.lower() for part in target.parts]
    return bool(set(names) & (SENSITIVE_PARTS | SENSITIVE_NAMES)) or any(
        name.startswith(".env.") or name.endswith((".pem", ".key")) for name in names
    )


def classify(call, scope, resolve_path):
    """Map a call to (scope_key, reason) without inspecting command text."""
    if call.name == "apply_patch":
        return SCOPE_FILE_WRITE, "审阅并批准多文件补丁"
    if call.name in FILE_TOOLS:
        target = resolve_path(call.arguments.get("path", "."))
        if not target.is_relative_to(scope.workspace):
            return SCOPE_OUTSIDE, "访问工作区外的文件"
        if sensitive_path(target):
            return SCOPE_SENSITIVE, "访问凭据、版本控制或 Agent 指令文件"
        if call.name in FILE_WRITES:
            return SCOPE_FILE_WRITE, "修改文件需要批准"
        return SCOPE_FILE_READ, None
    if call.name == "bash":
        cwd = resolve_path(call.arguments.get("cwd", "."))
        if not cwd.is_relative_to(scope.workspace) or sensitive_path(cwd):
            return SCOPE_OUTSIDE, "在工作区外或敏感目录执行命令"
        # No command is inferred safe from its text; every command is
        # classified identically by tool and cwd alone.
        return SCOPE_TERMINAL, "命令可能访问网络、修改文件或产生其他副作用"
    if call.name in WEB_TOOLS:
        return SCOPE_INTERNET, "联网检索会向外部服务发出请求并读取网页"
    if call.name.startswith("mcp__"):
        return SCOPE_INTERNET, "外部服务可能访问网络或产生副作用"
    if call.name in MUTATING_TOOLS:
        return SCOPE_PERSISTENT, "修改持久状态需要批准"
    return SCOPE_FILE_READ, None


def approval_reason(call, session, scope, resolve_path):
    mode = session.permission_mode
    scope_key, reason = classify(call, scope, resolve_path)
    if policy_for(mode, scope_key) == "exempt":
        return None
    return reason or "该操作需要批准"


def approval_scope(call, session, scope, resolve_path):
    """Return (scope_key, decision, reusable) for the session mode."""
    scope_key, _ = classify(call, scope, resolve_path)
    decision = policy_for(session.permission_mode, scope_key)
    if call.name == "apply_patch" and decision != "exempt":
        return scope_key, "per_call", False
    return scope_key, decision, reusable(session.permission_mode, scope_key)
