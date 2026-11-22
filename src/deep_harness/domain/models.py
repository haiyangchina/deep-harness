"""Immutable identities and results shared by application services."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PermissionMode = Literal["ask", "auto", "full"]
Harness = Literal["general", "coding", "research", "research_web"]


class DomainError(Exception):
    """An expected domain refusal with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ProfileScope:
    profile_id: str
    home: Path
    workspace: Path
    state_root: Path
    server_mode: str | None = None
    protected_paths: tuple[Path, ...] = ()
    executor_revision: str | None = None


@dataclass(frozen=True)
class Session:
    id: str
    profile_id: str
    principal: str
    prompt: str
    tools: tuple[str, ...]
    engine: Literal["standard", "deep"]
    harness: Harness = "general"
    permission_mode: PermissionMode = "ask"
    permission_revision: int = 0


@dataclass(frozen=True)
class Admission:
    id: str
    session_id: str
    text: str
    status: str
    generation: int
    result: str | None = None


@dataclass(frozen=True)
class ExecutionClaim:
    admission: Admission
    owner: str


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    status: str = "success"
