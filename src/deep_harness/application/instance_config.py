"""Operator-owned configuration for one browser-accessible Harness instance."""

import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from deep_harness.application.config import load_model_settings
from deep_harness.server_config import number

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")]
Capability = Literal["text", "streaming", "tool_calls", "structured_output"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def http_url(value, *, origin=False):
    result = urlsplit(value)
    if (
        result.scheme not in {"http", "https"}
        or not result.hostname
        or result.username
        or result.password
        or result.query
        or result.fragment
        or (origin and result.path not in {"", "/"})
    ):
        raise ValueError("Expected an HTTP(S) endpoint without credentials")
    _ = result.port
    return value.rstrip("/")


class WebSettings(Settings):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    trusted_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    public_origin: str = "http://127.0.0.1:8765"

    @model_validator(mode="after")
    def validate_boundary(self):
        origin = urlsplit(http_url(self.public_origin, origin=True))
        if not self.trusted_hosts or any(
            not re.fullmatch(r"[A-Za-z0-9.-]+", host) for host in self.trusted_hosts
        ):
            raise ValueError("Trusted hosts must be explicit hostnames or IPv4 addresses")
        if origin.hostname not in self.trusted_hosts:
            raise ValueError("Public origin must be included in trusted_hosts")
        return self


class InstanceLimits(Settings):
    active_runs: int = Field(default=4, ge=1, le=64)
    queued_runs: int = Field(default=64, ge=1, le=4096)
    max_upload_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)


class WorkspaceSettings(Settings):
    path: Path
    mode: Literal["read_only", "sandbox"] = "read_only"
    mcp_tools: tuple[str, ...] = ()


class ModelDefinition(Settings):
    adapter: Literal["ollama", "openai", "deepseek", "diagnostic"]
    model: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=120)
    base_url: str | None = None
    secret_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    secret_file: Path | None = None
    capabilities: frozenset[Capability] = frozenset({"text"})
    context_window_tokens: int = Field(default=32768, ge=4096, le=1_000_000)
    request_timeout_seconds: int = Field(
        default=180, ge=1, le=number("model.request_timeout_seconds_max")
    )
    concurrent_requests: int = Field(default=1, ge=1, le=32)
    reasoning: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self):
        if self.base_url is not None:
            http_url(self.base_url)
        if self.secret_env and self.secret_file:
            raise ValueError("Choose one model credential source")
        if self.adapter in {"openai", "deepseek"} and not (self.secret_env or self.secret_file):
            raise ValueError("A remote model endpoint requires a credential reference")
        return self

    def model_settings(self):
        return load_model_settings(
            provider=self.adapter,
            name=self.model,
            base_url=self.base_url,
            context_window_tokens=self.context_window_tokens,
            request_timeout_seconds=self.request_timeout_seconds,
            reasoning=self.reasoning,
        )


class AgentDefinition(Settings):
    label: str | None = Field(default=None, max_length=120)
    instructions: str = Field(min_length=1, max_length=16000)
    model: Identifier | None = None
    tools: tuple[str, ...] = ()
    required_capabilities: frozenset[Capability] = frozenset({"text"})
    harness: Literal["general", "coding", "research", "research_web"] = "general"
    workflow: Identifier | None = None


class SandboxSettings(Settings):
    image: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9/_.:@-]*$")
    memory_mb: int = Field(default=256, ge=64, le=4096)
    cpus: float = Field(default=1, gt=0, le=8)
    pids: int = Field(default=64, ge=8, le=512)
    max_workspaces: int = Field(default=8, ge=1, le=32)


class ConnectorDefinition(Settings):
    url: str
    description: str = Field(min_length=1, max_length=2000)
    schema_: Annotated[dict, Field(alias="schema")]
    result_schema: dict | None = None
    secret_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    secret_file: Path | None = None
    method: Literal["GET", "POST"] = "POST"
    writes: bool = False
    timeout_seconds: float = Field(default=15, gt=0, le=120)
    reconcile_url: str | None = None

    @model_validator(mode="after")
    def validate_endpoint(self):
        http_url(self.url)
        if self.secret_env and self.secret_file:
            raise ValueError("Choose one connector credential source")
        if self.reconcile_url:
            http_url(self.reconcile_url)
        if self.method == "GET" and self.writes:
            raise ValueError("A modifying connector must use POST")
        return self


class WorkflowStep(Settings):
    id: Identifier
    type: Literal["agent", "verify", "tool"]
    tool: str | None = None
    input_from: Identifier | None = None
    verifier: str | None = None
    schema_: Annotated[dict | None, Field(alias="schema")] = None
    approval: Literal["required"] | None = None
    on_unknown: Literal["reconcile"] | None = None


class WorkflowDefinition(Settings):
    steps: tuple[WorkflowStep, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_steps(self):
        seen = set()
        for step in self.steps:
            if step.id in seen or (step.input_from is not None and step.input_from not in seen):
                raise ValueError("Workflow steps must be unique and reference earlier steps")
            if step.type in {"verify", "tool"} and step.input_from is None:
                raise ValueError("Workflow verify and tool steps require input_from")
            if step.type == "tool" and not step.tool:
                raise ValueError("A tool step requires a registered tool")
            if step.type == "verify" and step.verifier != "json_schema" and not step.schema_:
                raise ValueError("Register a json_schema verifier or supply its schema")
            seen.add(step.id)
        return self


class InstanceSettings(Settings):
    web: WebSettings = WebSettings()
    limits: InstanceLimits = InstanceLimits()
    workspaces: dict[Identifier, WorkspaceSettings] = Field(min_length=1)
    models: dict[Identifier, ModelDefinition] = Field(min_length=1)
    agents: dict[Identifier, AgentDefinition] = Field(default_factory=dict)
    connectors: dict[str, ConnectorDefinition] = Field(default_factory=dict)
    workflows: dict[Identifier, WorkflowDefinition] = Field(default_factory=dict)
    sandbox: SandboxSettings | None = None
    default_workspace: Identifier
    default_model: Identifier

    @model_validator(mode="after")
    def validate_references(self):
        if self.default_workspace not in self.workspaces or self.default_model not in self.models:
            raise ValueError("Defaults must refer to a configured workspace and model")
        if self.sandbox is None and any(w.mode == "sandbox" for w in self.workspaces.values()):
            raise ValueError("Sandbox workspaces require an executor configuration")
        for name, agent in self.agents.items():
            if agent.model is not None and agent.model not in self.models:
                raise ValueError(f"Agent {name} references an unknown model")
            if agent.workflow is not None and agent.workflow not in self.workflows:
                raise ValueError(f"Agent {name} references an unknown workflow")
        return self


def load_instance_config(path: Path):
    instance = InstanceSettings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    workspaces = {}
    for name, workspace in instance.workspaces.items():
        target = (path.parent / workspace.path).resolve(strict=True)
        if not target.is_dir():
            raise ValueError(f"Workspace {name} must be a directory")
        workspaces[name] = workspace.model_copy(update={"path": target})
    overrides = {"workspaces": workspaces}
    for kind in ("models", "connectors"):
        values = getattr(instance, kind)
        overrides[kind] = {
            name: row.model_copy(update={"secret_file": (path.parent / row.secret_file).resolve()})
            if row.secret_file
            else row
            for name, row in values.items()
        }
    return instance.model_copy(update=overrides)
