"""Construct or reuse the LangChain model selected by a launch or session."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from deep_harness.application.config import ModelSettings
from deep_harness.domain.identity import fingerprint
from deep_harness.domain.models import DomainError
from deep_harness.infrastructure.credentials import read_secret
from deep_harness.runtime.models import DiagnosticModel, deepseek_model, ollama_model, openai_model


def default_vision(settings):
    if settings.vision is not None:
        return settings.vision
    return settings.provider == "ollama" and settings.name.split(":", 1)[0] == "qwen3.5"


def open_model(
    settings: ModelSettings,
    secret_env="OPENAI_API_KEY",
    *,
    remote_window=False,
    secret_file=None,
):
    if settings.provider == "diagnostic":
        return DiagnosticModel()
    common = dict(
        model=settings.name,
        base_url=settings.base_url,
        request_timeout=settings.request_timeout_seconds,
    )
    if settings.provider == "ollama":
        model = ollama_model(
            **common,
            context_window_tokens=settings.context_window_tokens,
            reasoning=settings.reasoning,
        )
        model.vision = default_vision(settings)
        return model
    api_key = read_secret(secret_env, secret_file)
    if not api_key:
        raise DomainError(
            "model_secret_missing",
            "Required model credential is missing"
            if secret_file
            else f"Required secret variable is missing: {secret_env}",
        )
    if settings.provider == "deepseek":
        return deepseek_model(
            **common,
            api_key=api_key,
            context_window_tokens=settings.context_window_tokens,
            reasoning=settings.reasoning,
        )
    model = openai_model(
        **common,
        api_key=api_key,
        context_window_tokens=settings.context_window_tokens if remote_window else None,
    )

    model.vision = default_vision(settings)
    return model


@dataclass(frozen=True)
class CatalogEntry:
    label: str
    settings: ModelSettings
    secret_env: str | None
    capabilities: frozenset[str]
    concurrent_requests: int = 1
    secret_file: Path | None = None


class ModelCatalog:
    def __init__(self, entries, default, *, factory=None):
        self.entries = entries
        self.default = default
        self.factory = factory or self.open_entry
        self.models = {}
        self.slots = {
            key: asyncio.Semaphore(row.concurrent_requests) for key, row in entries.items()
        }

    @staticmethod
    def open_entry(entry):
        return open_model(
            entry.settings, entry.secret_env, remote_window=True, secret_file=entry.secret_file
        )

    def capabilities(self, model_id):
        entry = self.entries.get(model_id or self.default)
        if entry is None:
            return frozenset()
        if entry.settings.provider not in {"ollama", "openai"}:
            return entry.capabilities - {"vision"}
        return entry.capabilities | ({"vision"} if default_vision(entry.settings) else set())

    def get(self, model_id, *, tools=False, required=()):
        model_id = model_id or self.default
        entry = self.entries.get(model_id)
        if entry is None:
            raise DomainError("model_not_found", "The selected model is not configured")
        needed = set(required)
        if tools:
            needed.add("tool_calls")
        supported = self.capabilities(model_id)
        if needed - supported:
            raise DomainError(
                "model_capability",
                "Model lacks these capabilities: " + ", ".join(sorted(needed - supported)),
            )
        if model_id not in self.models:
            self.models[model_id] = self.factory(entry)
        return self.models[model_id]

    def list(self):
        return [
            {
                "id": key,
                "label": entry.label,
                "provider": entry.settings.provider,
                "model": entry.settings.name,
                "capabilities": sorted(self.capabilities(key)),
                "configured": (
                    entry.settings.provider not in {"openai", "deepseek"}
                    or bool(os.environ.get(entry.secret_env or ""))
                    or bool(entry.secret_file and entry.secret_file.is_file())
                ),
            }
            for key, entry in self.entries.items()
        ]

    def revision(self, model_id):
        entry = self.entries[model_id]
        return fingerprint(
            {
                "settings": entry.settings.model_dump(
                    exclude={"vision"} if entry.settings.vision is None else None
                ),
                "capabilities": sorted(entry.capabilities),
            }
        )
