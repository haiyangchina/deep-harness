"""The single source of truth for harness capabilities and tool grants.

A capability is a product contract, not a user toggle: core capabilities are
granted to every new session and cannot be switched off in the UI. Extensions
are opt-in. Approval policy, workspace boundaries and the effect ledger remain
separate, always-on constraints that no capability can bypass.

Access scopes and the three permission modes are declared here too, so the
backend policy, HTTP manifest, CLI and desktop UI all read one description.
"""

from dataclasses import asdict, dataclass
from typing import Literal

from deep_harness.application.harness_spec import HARNESSES, spec_for
from deep_harness.application.harness_spec import manifest as harness_manifest
from deep_harness.domain.models import DomainError


@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    label: str
    description: str
    tools: tuple[str, ...]
    tier: Literal["core", "extension"]
    risk: Literal["read", "write", "command"]
    approval_hint: str


@dataclass(frozen=True)
class AccessScope:
    key: str
    label: str
    description: str
    scope_keys: tuple[str, ...]
    approval_hint: str


@dataclass(frozen=True)
class PermissionModeSpec:
    key: str
    label: str
    description: str
    approval: str
    requires_acknowledgement: bool


# Web retrieval is a core capability: it is read-only, so it joins
# READ_ONLY_CORE and is inherited by research harnesses and delegated children.
WEB_TOOLS: tuple[str, ...] = (
    "web_search",
    "web_fetch",
    "web_extract",
    "web_research",
)

CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        key="file_read",
        label="文件读取与检索",
        description="读取工作区文件、递归列出目录、按字面文本搜索符号。",
        tools=("read", "list_files", "search_files"),
        tier="core",
        risk="read",
        approval_hint="工作区内只读访问免批准；工作区外与敏感路径按批准模式处理。",
    ),
    CapabilitySpec(
        key="file_write",
        label="文件写入与编辑",
        description="创建或覆盖 UTF-8 文件，对唯一匹配做精确替换。",
        tools=("write", "edit"),
        tier="core",
        risk="write",
        approval_hint="按批准模式逐次确认；覆盖已有文件需要 expected_sha256。",
    ),
    CapabilitySpec(
        key="command_exec",
        label="本地命令执行",
        description="在工作区相对 cwd 执行 Shell 命令，返回退出码、标准输出与标准错误。",
        tools=("bash",),
        tier="core",
        risk="command",
        approval_hint="每条命令按批准模式逐次确认（full 模式除外），不区分命令用途。",
    ),
    CapabilitySpec(
        key="web_access",
        label="联网检索与深度研究",
        description=(
            "搜索公开网页、读取并清洗页面正文、批量抓取，并在一次调用内并发多查询研究，"
            "返回带引用编号的有界证据包。"
        ),
        tools=WEB_TOOLS,
        tier="core",
        risk="read",
        approval_hint=(
            "外部访问除完全访问外逐次确认；网页正文标记为不可信证据，输出按字符预算封顶。"
        ),
    ),
)

CORE_TOOLS: tuple[str, ...] = tuple(
    sorted({tool for cap in CAPABILITIES if cap.tier == "core" for tool in cap.tools})
)
READ_ONLY_CORE: tuple[str, ...] = tuple(
    sorted(
        {
            tool
            for cap in CAPABILITIES
            if cap.tier == "core" and cap.risk == "read"
            for tool in cap.tools
        }
    )
)
EXTENSION_TOOLS: tuple[str, ...] = (
    "save_memory",
    "delegate_task",
    "check_delegation",
    "apply_patch",
)
CODING_SESSION_TOOLS = (
    "apply_patch", "delegate_task", "check_delegation", "project_instructions", "symbols",
    "read_files",
    "merge_delegations", "code_navigation",
)
CAPABILITY_VERSION = 2

# Harness names and tier attributes come from harness_spec, the single source of
# truth. derive_grants(), session creation, CLI and HTTP all enforce the same rule.
READ_ONLY_HARNESSES: frozenset[str] = frozenset(
    spec.key for spec in (spec_for(name) for name in HARNESSES) if spec.read_only
)

MUTATING_EXTENSIONS = frozenset(
    {"save_memory", "write", "edit", "bash", "apply_patch", "merge_delegations"}
)


# Approval scopes are risk boundaries, not tool grants.
SCOPE_FILE_READ = "file_read:workspace"
SCOPE_FILE_WRITE = "file_write:workspace"
SCOPE_TERMINAL = "terminal:workspace"
SCOPE_OUTSIDE = "outside:workspace"
SCOPE_SENSITIVE = "sensitive:path"
SCOPE_INTERNET = "internet:external"
SCOPE_PERSISTENT = "persistent:state"

ALL_SCOPE_KEYS: tuple[str, ...] = (
    SCOPE_FILE_READ,
    SCOPE_FILE_WRITE,
    SCOPE_TERMINAL,
    SCOPE_OUTSIDE,
    SCOPE_SENSITIVE,
    SCOPE_INTERNET,
    SCOPE_PERSISTENT,
)

ACCESS_SCOPES: tuple[AccessScope, ...] = (
    AccessScope(
        key="file_operations",
        label="文件操作",
        description="读取、创建、修改、上传、删除此计算机上任意位置的文件。",
        scope_keys=(SCOPE_FILE_READ, SCOPE_FILE_WRITE, SCOPE_OUTSIDE, SCOPE_SENSITIVE),
        approval_hint="工作区内只读免批；写入与工作区外访问除完全访问外逐次确认。",
    ),
    AccessScope(
        key="terminal_commands",
        label="终端命令",
        description="运行命令、安装软件或修改系统设置。",
        scope_keys=(SCOPE_TERMINAL, SCOPE_OUTSIDE),
        approval_hint="每条命令按批准模式确认；后端不从命令文本推断安全性。",
    ),
    AccessScope(
        key="internet_access",
        label="访问互联网",
        description="搜索公开网页、读取外部页面、发送数据或使用已启用的连接器。",
        scope_keys=(SCOPE_INTERNET, SCOPE_PERSISTENT),
        approval_hint="内置联网工具、外部服务与持久状态写入除完全访问外逐次确认。",
    ),
)

# Only these scopes may reuse one approval inside a single admission. Border
# crossings never reuse an approval in any non-full mode.
REUSABLE_SCOPES: frozenset[str] = frozenset({SCOPE_FILE_WRITE})
ALWAYS_PER_CALL: frozenset[str] = frozenset(
    {SCOPE_TERMINAL, SCOPE_OUTSIDE, SCOPE_SENSITIVE, SCOPE_INTERNET, SCOPE_PERSISTENT}
)
READ_ONLY_SCOPES: frozenset[str] = frozenset({SCOPE_FILE_READ})

# "exempt" never asks; "per_round" asks once per admission for that scope;
# "per_call" asks for every invocation.
PERMISSION_POLICY: dict[str, dict[str, str]] = {
    "ask": {"*": "per_call", SCOPE_FILE_READ: "exempt"},
    "auto": {
        "*": "per_call",
        SCOPE_FILE_READ: "exempt",
        SCOPE_FILE_WRITE: "per_round",
    },
    "full": {"*": "exempt"},
}

PERMISSION_MODES: tuple[PermissionModeSpec, ...] = (
    PermissionModeSpec(
        key="ask",
        label="请求批准",
        description="文件操作、终端命令与访问互联网每次逐项询问。",
        approval="每次逐项",
        requires_acknowledgement=False,
    ),
    PermissionModeSpec(
        key="auto",
        label="帮我批准",
        description="同一轮的普通文件写入可以复用批准。终端、工作区外与互联网逐次确认。",
        approval="同一轮每类一次",
        requires_acknowledgement=False,
    ),
    PermissionModeSpec(
        key="full",
        label="完全访问权限",
        description="三类能力全部免批；启用前需勾选风险确认。",
        approval="免批",
        requires_acknowledgement=True,
    ),
)

HARD_BOUNDARIES: tuple[str, ...] = (
    "运行状态目录对所有档位一律拒绝，full 也不例外",
    "full 不授予会话未启用的工具，research / research_web 不因 full 获得写入或命令",
    "工具授权在创建会话时冻结，切换模式不改写历史消息或模型快照",
    "未知执行结果不自动重试，子任务继承受限交集",
    "批准记录按调用与参数 digest 独立保存，复用只在同一轮任务内按作用域生效",
    "联网返回的网页正文一律作为不可信证据，不进入系统提示或长期记忆；私有网络地址默认拒绝",
)


def policy_for(mode: str, scope_key: str) -> str:
    table = PERMISSION_POLICY.get(mode, PERMISSION_POLICY["ask"])
    return table.get(scope_key, table["*"])


def reusable(mode: str, scope_key: str) -> bool:
    return policy_for(mode, scope_key) == "per_round" and scope_key in REUSABLE_SCOPES


def permission_policy() -> dict:
    """Serializable permission contract consumed by HTTP and desktop."""
    return {
        "modes": [asdict(mode) for mode in PERMISSION_MODES],
        "access_scopes": [
            {**asdict(scope), "scope_keys": list(scope.scope_keys)} for scope in ACCESS_SCOPES
        ],
        "scope_keys": list(ALL_SCOPE_KEYS),
        "matrix": {
            mode: {key: policy_for(mode, key) for key in ALL_SCOPE_KEYS}
            for mode in ("ask", "auto", "full")
        },
        "reusable_scopes": sorted(REUSABLE_SCOPES),
        "always_per_call": sorted(ALWAYS_PER_CALL),
        "read_only_scopes": sorted(READ_ONLY_SCOPES),
        "hard_boundaries": list(HARD_BOUNDARIES),
        "requires_acknowledgement": [
            mode.key for mode in PERMISSION_MODES if mode.requires_acknowledgement
        ],
    }


def derive_grants(harness, extensions=(), mcp_tools=()):
    """Derive the frozen tool grant for a new session.

    Harnesses are a contract, not a user preference: every read-only harness
    receives only read-only core tools. ``full`` is an approval mode and never
    grants tools that were not enabled here. All harnesses now run on the single
    deepagents kernel, so delegation availability is a harness-tier property
    (declared in HarnessSpec) rather than an engine switch.
    """
    spec = spec_for(harness)
    read_only = spec.read_only
    granted = set(READ_ONLY_CORE if read_only else CORE_TOOLS)
    if harness == "coding":
        granted.update(CODING_SESSION_TOOLS)
    requested = list(extensions) + list(mcp_tools)
    for name in requested:
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                raise DomainError("mcp_not_found", "MCP tool name must be mcp__server__tool")
        elif name not in EXTENSION_TOOLS:
            raise DomainError("tool_denied", f"Unknown extension capability: {name}")
        if read_only and name in MUTATING_EXTENSIONS:
            raise DomainError("tool_denied", "Research harness cannot receive mutating tools")
        granted.add(name)
    if "delegate_task" in granted:
        granted.add("check_delegation")
    return tuple(sorted(granted))


def manifest():
    """Serializable capability description consumed by CLI, HTTP and desktop."""
    return {
        "capability_version": CAPABILITY_VERSION,
        "capabilities": [
            {
                "key": cap.key,
                "label": cap.label,
                "description": cap.description,
                "tier": cap.tier,
                "risk": cap.risk,
                "tools": list(cap.tools),
                "approval_hint": cap.approval_hint,
            }
            for cap in CAPABILITIES
        ],
        "core_tools": list(CORE_TOOLS),
        "read_only_core": list(READ_ONLY_CORE),
        "harnesses": list(HARNESSES),
        "read_only_harnesses": sorted(READ_ONLY_HARNESSES),
        "extensions": list(EXTENSION_TOOLS),
        "coding_session_tools": list(CODING_SESSION_TOOLS),
        "access_scopes": permission_policy()["access_scopes"],
        "permission_modes": permission_policy()["modes"],
        "permission_policy": permission_policy(),
        "hard_boundaries": list(HARD_BOUNDARIES),
        "harness_specs": harness_manifest()["specs"],
        "context_policy": harness_manifest()["context_policy"],
    }
